import re
import logging
from typing import Dict, Any
from surat.models import SuratType
from surat.agents.slot_memory import SlotMemoryManager, is_value_filled

logger = logging.getLogger(__name__)

def handle_switch_confirmation_redis(user_message: str, thread_id: str, session_data: dict, thread_obj) -> str:
    """Menangani konfirmasi saat mahasiswa ingin beralih jenis surat di tengah jalan[cite: 1]."""
    message_lower = user_message.strip().lower()
    
    if message_lower in ["ya", "iya", "y", "beralih", "lanjutkan", "lanjut", "oke", "ok", "ganti", "switch"]:
        old_code = session_data.get("intent")
        new_code = session_data.get("pending_intent")
        
        session_data["intent"] = new_code
        session_data["pending_intent"] = None
        session_data["is_waiting_for_switch_confirmation"] = False
        session_data["slots"] = {}  # Reset slot data lama secara bersih
        
        new_surat_obj = SuratType.objects.get(kode_surat=new_code)
        
        if thread_obj:
            thread_obj.document_type = new_code
            thread_obj.title = f"Permohonan {new_surat_obj.nama_surat}"
            thread_obj.save(update_fields=['document_type', 'title', 'updated_at'])
            
        return f"Draf sebelumnya ({old_code}) telah dibatalkan. Berhasil beralih ke **{new_surat_obj.nama_surat}**. Silakan masukkan data yang diperlukan!"
    
    else:
        session_data["pending_intent"] = None
        session_data["is_waiting_for_switch_confirmation"] = False
        active_surat_obj = SuratType.objects.get(kode_surat=session_data.get("intent"))
        return f"Baik, peralihan dibatalkan. Kita tetap melanjutkan pengisian untuk **{active_surat_obj.nama_surat}**."


def handle_destination_type_selection(user_message: str, session_data: dict, current_slots: dict) -> str:
    """Menangkap pilihan jenis destinasi dokumen (Email atau Telegram)."""
    msg_lower = user_message.lower()
    
    if "email" in msg_lower:
        current_slots["chosen_destination"] = "email"
        session_data["slots"] = current_slots
        return "Baik, terpilih **Email**. Mohon masukkan alamat email tujuan Anda saat ini:"
    elif "telegram" in msg_lower:
        current_slots["chosen_destination"] = "telegram"
        session_data["slots"] = current_slots
        return "Baik, terpilih **Telegram**. Mohon masukkan nomor **Chat ID Telegram** Anda:"
    else:
        return (
            "🎉 **Semua data persyaratan surat sudah lengkap 100%!**\n\n"
            "Terakhir, dokumen PDF hasil jadi nanti mau dikirimkan ke mana setelah disetujui dosen?\n"
            "👉 Silakan ketik pilihan Anda: **Email** atau **Telegram**"
        )


def handle_destination_address_input(user_message: str, session_data: dict, current_slots: dict, thread_id: str) -> str:
    """Menangkap alamat tujuan, lalu mengubah sub_state ke konfirmasi akhir di orkestrator."""
    address = user_message.strip()
    current_slots["target_destination_address"] = address
    session_data["slots"] = current_slots
    
    from .session_manager import AutoSuratSessionManager
    # Ubah sub_state ke konfirmasi akhir, serahkan kontrol tampilan ke orkestrator
    session_data["sub_state"] = "WAITING_SLOT_CONFIRMATION"
    AutoSuratSessionManager.save_session(thread_id, session_data)
    
    # Buat ringkasan lengkap untuk ditampilkan di pesan konfirmasi
    summary_str = "\n".join([f"- **{k}**: {v}" for k, v in current_slots.items()])
    
    return (
        f"✅ Preferensi pengiriman disimpan.\n\n"
        f"📋 **Ringkasan Data Permohonan:**\n{summary_str}\n\n"
        f"✨ *Apakah data di atas sudah benar dan siap dibuatkan draf suratnya? (Jawab: **Ya** / **Belum**)*"
    )


def handle_destination_selection(user_message: str, session_data: dict, current_slots: dict, thread_id: str) -> str:
    """Router internal untuk penanganan destinasi akhir."""
    if not current_slots.get("chosen_destination"):
        return handle_destination_type_selection(user_message, session_data, current_slots)
    
    if not current_slots.get("target_destination_address"):
        return handle_destination_address_input(user_message, session_data, current_slots, thread_id)
    
    return "Silakan konfirmasikan apakah data sudah benar dengan mengetik **'Ya'** atau **'Belum'**."


def process_slot_filling_redis(user_message: str, session_data: dict) -> str:
    """Ekstraksi slot bebas, sanitasi regex, dan pengecekan kelengkapan[cite: 1]."""
    try:
        surat_type = SuratType.objects.get(kode_surat=session_data.get("intent"))
    except SuratType.DoesNotExist:
        return "Terjadi kesalahan sistem: Jenis surat tidak ditemukan."

    required_fields = surat_type.required_payload_fields or []
    current_slots = session_data.get("slots", {})
    
    slot_mgr = SlotMemoryManager(
        kode_surat=surat_type.kode_surat,
        required_fields=required_fields,
        initial_slots=current_slots
    )

    # Jika slot utama sudah lengkap, lanjut ke pengisian destinasi (email/telegram)
    if slot_mgr.is_complete():
        thread_id = session_data.get("thread_id") or ""
        return handle_destination_selection(user_message, session_data, current_slots, thread_id)

    # Ekstraksi slot menggunakan AI / Extractor
    extracted_data = {}
    try:
        from surat.agents.letter_agent import SuratDraftingAgent
        agent = SuratDraftingAgent()
        extraction_result = agent.extract_slots_from_text(
            user_prompt_context=user_message,
            kode_surat=surat_type.kode_surat,
            required_fields=required_fields
        )
        extracted_data = getattr(extraction_result, "extracted_slots", {}) or {}
    except Exception:
        # missing = slot_mgr.get_missing_fields()
        # if missing:
        #     first_missing_key = missing[0].get("field")
        #     extracted_data = {first_missing_key: user_message}
        logger.warning(f"Gagal mengekstrak slot dari teks: {logging.exception}. Menggunakan fallback heuristik.")
        extracted_data = {}

    # Sanitasi data hasil ekstraksi
    cleaned_extracted_data = {}
    for field_key, raw_val in extracted_data.items():
        if is_value_filled(raw_val):
            cleaned_extracted_data[field_key] = sanitize_extracted_slot(field_key, raw_val)

    # Gabungkan ke memori sesi secara deterministik
    updated_slots = SlotMemoryManager.merge_slots(slot_mgr.accumulated_slots, cleaned_extracted_data)
    slot_mgr.accumulated_slots = updated_slots
    session_data["slots"] = updated_slots

    # Jika setelah digabungkan ternyata sudah lengkap 100%
    if slot_mgr.is_complete():
        thread_id = session_data.get("thread_id") or ""
        return handle_destination_selection(user_message, session_data, updated_slots)

    # checklist_lines, _, _ = generate_checklist_status_response(surat_type, updated_slots)
    # formatted_checklist = "\n".join(checklist_lines)
    
    missing_notes = slot_mgr.generate_clarification_notes()
    next_target_prompt = missing_notes[0] if missing_notes else "Mohon lengkapi data yang masih kurang."
    
    return f"Baik, data dicatat. Selanjutnya: {next_target_prompt}"

def generate_checklist_status_response(surat_type, current_slots: dict):
    required_fields = surat_type.required_payload_fields or []
    slot_mgr = SlotMemoryManager(
        kode_surat=surat_type.kode_surat,
        required_fields=required_fields,
        initial_slots=current_slots
    )
    
    checklist_lines = [f"📋 **Status Kelengkapan: {surat_type.nama_surat}**"]
    missing_fields = slot_mgr.get_missing_fields(current_slots)
    missing_field_keys = {item.get("field") for item in missing_fields}
    
    next_target_label = None
    
    for item in required_fields:
        field_key = item.get("field")
        field_label = item.get("label", field_key) 
        
        if field_key not in missing_field_keys:
            value = current_slots.get(field_key)
            checklist_lines.append(f"✅ **{field_label}**: {value}")
        else:
            checklist_lines.append(f"❌ **{field_label}**: *(Belum diisi)*")
            if not next_target_label:
                next_target_label = field_label  
                
    all_complete = slot_mgr.is_complete(current_slots)
    return checklist_lines, all_complete, next_target_label


def sanitize_extracted_slot(field_name: str, raw_value: Any) -> Any:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return raw_value

    text = raw_value.strip()
    if field_name == "instansi_tujuan":
        text = re.sub(r'^(untuk|ke|pada|kepada|di)\s+', '', text, flags=re.IGNORECASE).strip()

    text = text.strip('"\'`')
    text = re.sub(r'[.\s]+$', '', text)
    return text