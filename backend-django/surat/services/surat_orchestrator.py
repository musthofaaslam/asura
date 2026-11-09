import logging
import threading
import traceback
from typing import Dict, Any, Optional
from django.db import close_old_connections

from surat.agents.letter_agent import SuratDraftingAgent
from .detect_intent import detect_intent_dynamically
from .route_intent import route_intent_with_ai
from surat.models import SuratType, SuratRequest, ChatThread
from .slot_processor import handle_switch_confirmation_redis, process_slot_filling_redis
from surat.services.validators import run_validators
from surat.models import ChatThread, ChatMessage
from .session_manager import AutoSuratSessionManager

logger = logging.getLogger(__name__)


class SuratWorkflowOrchestrator:
    """
    Orkestrator Terpusat untuk Siklus Hidup Agentic AI Level 2 (Surat Kampus).
    Mengatur perpindahan state dari Collecting Slots -> AI Drafting -> User Review -> Redraft -> Gatekeeper.
    """

    @classmethod
    def process_incoming_chat(cls, thread_id: Optional[str], user_message: str, user_obj) -> Dict[str, Any]:
        # 1. Tangani Lazy Initialization jika thread_id kosong/null
        if not thread_id:
            new_thread = AutoSuratSessionManager.initialize_thread(user_obj, initial_title=user_message[:30])
            thread_id = str(new_thread.id)
        else:
            # Pastikan thread benar milik user yang aktif
            thread = ChatThread.objects.filter(id=thread_id, user=user_obj).first()
            if not thread:
                return {
                    "status": "error",
                    "message": "Sesi chat atau thread tidak ditemukan, atau Anda tidak memiliki hak akses."
                }
        # 2. Ambil sesi dari Redis
        session_data = AutoSuratSessionManager.get_session(thread_id, user_obj)
        state = session_data.get("state", "ACTIVE")
        sub_state = session_data.get("sub_state", "WAITING_FOR_INTENT") # Default awal untuk chat baru

        logger.info(f"[Orchestrator] Thread {thread_id} - State: {state} | SubState: {sub_state}")

        # 3. Validasi status sesi (Expired / Cancelled)
        if state in ["EXPIRED", "CANCELLED", "NOT_FOUND"]:
            return cls._handle_invalid_session(thread_id, state)

        try:
            thread_obj = ChatThread.objects.filter(id=thread_id).first()
            if thread_obj:
                ChatMessage.objects.create(
                    thread=thread_obj,
                    sender_type=ChatMessage.SenderType.USER,
                    message=user_message
                )
        except Exception as db_exc:
            logger.error("Gagal mencatat pesan user ke database ChatMessage: %s", db_exc)

        # 5. Interceptor Global: Cek kata kunci pembatalan cepat
        if user_message.lower().strip() in ["batal", "cancel", "reset", "stop"]:
            AutoSuratSessionManager.clear_session(thread_id, target_status="CANCELLED")
            return {
                "status": "success",
                "state": "CANCELLED",
                "thread_id": thread_id,
                "message": "Sesi permohonan surat telah dibatalkan. Ketik pesan baru jika ingin memulai lagi."
            }
            
        # 4. ROUTING BERDASARKAN SUB-STATE & MAIN STATE
        response_result = {}

        if sub_state == "WAITING_FOR_INTENT":
            # Tahap Awal: Deteksi jenis surat (intent) dan tujuan pengiriman
            response_result = cls._handle_intent_detection(thread_id, user_message, user_obj, session_data)

        elif sub_state == "NORMAL_COLLECTION":
            # Tahap Tanya-Jawab Pengumpulan Slot
            response_result = cls._handle_slot_filling(thread_id, user_message, user_obj, session_data)

        elif sub_state == "WAITING_SLOT_CONFIRMATION":
            # Tahap Konfirmasi Data Lengkap (Ya/Belum)
            response_result = cls._handle_slot_confirmation_response(thread_id, user_message, user_obj, session_data)

        elif sub_state == "WAITING_FOR_REVISION" or state in ["PENDING_APPROVAL", "REVISION_REQUIRED"]:
            # Tahap Review Draf atau Pemberian Catatan Revisi
            response_result = cls._handle_chat_review_or_redraft(thread_id, user_message, user_obj, session_data)

        elif state == "IN_REVIEW":
            # Tahap Persetujuan Berjenjang oleh Dosen/Pejabat
            response_result = {
                "status": "waiting",
                "message": "Permohonan surat kamu sedang dalam tinjauan/persetujuan berjenjang. Mohon menunggu."
            }

        # 5. Pastikan thread_id selalu dikembalikan ke frontend
        if isinstance(response_result, dict):
            response_result["thread_id"] = thread_id

        return response_result
    
    @classmethod
    def _handle_invalid_session(cls, thread_id: str, state: str) -> Dict[str, Any]:
        if state == "EXPIRED":
            return {
                "status": "error",
                "message": "Sesi chat kamu sudah kadaluarsa. Silakan mulai percakapan baru.",
                "thread_id": thread_id
            }
        elif state == "CANCELLED":
            return {
                "status": "error",
                "message": "Sesi chat kamu telah dibatalkan. Silakan mulai percakapan baru.",
                "thread_id": thread_id
            }
        else:
            return {
                "status": "error",
                "message": "Sesi chat tidak ditemukan. Silakan mulai percakapan baru.",
                "thread_id": thread_id
            }
            
    @classmethod
    def _handle_intent_detection(cls, thread_id: str, user_message: str, user_obj, session_data: dict) -> Dict[str, Any]:
        """
        Tahap 1: Mendeteksi jenis surat (intent) dari pesan pertama user menggunakan 
        modul detect_intent dan route_intent yang sudah ada.
        """
        cleaned_msg = user_message.strip()

        # 1. Deteksi intent (Lapis 1: Keyword/Alias -> Lapis 2: AI Router)
        detected_kode_surat = detect_intent_dynamically(cleaned_msg)
        if not detected_kode_surat:
            detected_kode_surat = route_intent_with_ai(cleaned_msg)

        # 2. Jika tetap tidak dikenali, minta klarifikasi ke user
        if not detected_kode_surat:
            return {
                "status": "need_clarification",
                "message": "Mohon maaf, jenis surat yang Anda maksud belum dikenali. Silakan ketik nama atau jenis surat dengan lebih jelas."
            }

        # 3. Jika intent ketemu, simpan ke sesi Redis & update ChatThread di PostgreSQL
        try:
            surat_obj = SuratType.objects.get(kode_surat=detected_kode_surat)
        except SuratType.DoesNotExist:
            return {
                "status": "error",
                "message": f"Jenis surat dengan kode '{detected_kode_surat}' tidak ditemukan di sistem."
            }

        session_data["intent"] = detected_kode_surat
        session_data["sub_state"] = "NORMAL_COLLECTION"  # Geser sub-state ke pengumpulan slot
        session_data["slots"] = {} # Inisialisasi slot kosong

        # Sinkronisasi ke model ChatThread di database
        thread_obj = ChatThread.objects.filter(id=thread_id).first()
        if thread_obj:
            thread_obj.document_type = detected_kode_surat
            thread_obj.title = f"Permohonan {surat_obj.nama_surat}"
            thread_obj.save(update_fields=['document_type', 'title', 'updated_at'])

        # Simpan perubahan ke Redis via SessionManager
        AutoSuratSessionManager.save_session(thread_id, session_data)

        # 4. Berikan respons selamat datang sekaligus menagih slot pertama
        required_fields = surat_obj.required_payload_fields or []
        if required_fields:
            first_field = required_fields[0]
            first_label = first_field.get("label", first_field.get("field", "data utama"))
        else:
            first_label = "data yang diperlukan"

        welcome_msg = (
            f"Siap! Saya bantu proses **{surat_obj.nama_surat}** ({detected_kode_surat}) ya.\n\n"
            f"Untuk memulainya, boleh informasikan **{first_label}** terlebih dahulu?"
        )

        return {
            "status": "success",
            "message": welcome_msg
        }
        
    @classmethod
    def _handle_slot_filling(cls, thread_id: str, user_message: str, user_obj, session_data: dict) -> Dict[str, Any]:
        cleaned_msg = user_message.strip()
        active_intent = session_data.get("intent")

        # 1. Cek apakah user sedang dalam proses konfirmasi switch intent (pindah surat)
        if session_data.get("is_waiting_for_switch_confirmation"):
            thread_obj = ChatThread.objects.filter(id=thread_id).first()
            reply_msg = handle_switch_confirmation_redis(cleaned_msg, thread_id, session_data, thread_obj)
            AutoSuratSessionManager.save_session(thread_id, session_data)
            return {"status": "success", "message": reply_msg}

        # 2. Cek apakah di tengah jalan user menyebutkan intent/surat baru
        detected_kode_surat = detect_intent_dynamically(cleaned_msg)
        if not detected_kode_surat:
            detected_kode_surat = route_intent_with_ai(cleaned_msg)

        if detected_kode_surat and detected_kode_surat != active_intent:
            old_surat = SuratType.objects.get(kode_surat=active_intent)
            new_surat = SuratType.objects.get(kode_surat=detected_kode_surat)
            
            session_data["pending_intent"] = detected_kode_surat
            session_data["is_waiting_for_switch_confirmation"] = True
            AutoSuratSessionManager.save_session(thread_id, session_data)
            
            return {
                "status": "success",
                "message": (
                    f"⚠️ Sesi saat ini sedang memproses **{old_surat.nama_surat}**. "
                    f"Apakah Anda ingin membatalkan draf tersebut dan beralih membuat **{new_surat.nama_surat}**? "
                )
            }

        # 3. Jalankan fungsi utama slot filling 
        reply_msg = process_slot_filling_redis(cleaned_msg, session_data)
        
        # 🔑 WAJIB: Simpan kembali session_data yang sudah diperbarui ke Redis di tingkat orkestrator!
        AutoSuratSessionManager.save_session(thread_id, session_data)
        
        return {"status": "success", "message": reply_msg}
    
    @classmethod
    def _handle_slot_confirmation_response(cls, thread_id: str, user_message: str, user_obj, session_data: dict) -> Dict[str, Any]:
        """Tahap Konfirmasi Akhir (Ya / Belum) sebelum AI Drafting dipicu."""
        positive_keywords = ["ya", "oke", "ok", "benar", "lanjut", "siap", "siyap"]
        is_approved = any(kw in user_message.lower() for kw in positive_keywords)

        if is_approved:
            kode_surat = session_data.get("intent")
            slots = session_data.get("slots", {})

            # 0. Ekstraksi Data Internal Mahasiswa & Jalankan Validasi Akademik
            try:
                student_obj = user_obj.student_profile
            except AttributeError:
                return {
                    "status": "error",
                    "message": "❌ Akun ini tidak memiliki profil mahasiswa yang terikat."
                }

            student_data = {
                "nim": student_obj.nim,
                "nama": getattr(user_obj, "nama", ""),
                "status_akademik": student_obj.status_akademik,
                "status_pembayaran_ukt": student_obj.status_pembayaran_ukt,
                "semester_aktif": student_obj.semester_aktif,
                "total_sks_lulus": student_obj.total_sks_lulus,
                "ipk_kumulatif": student_obj.ipk_kumulatif,
                "catatan_pelanggaran_etik": student_obj.catatan_pelanggaran_etik,
                "status_disiplin_kampus": student_obj.status_disiplin_kampus,
                "status_beasiswa_aktif": student_obj.status_beasiswa_aktif,
                "nama_beasiswa_aktif": student_obj.nama_beasiswa_aktif,
                "prodi": student_obj.prodi.nama_prodi if student_obj.prodi else "",
                "fakultas": student_obj.fakultas.nama_fakultas if student_obj.fakultas else "",
            }

            student_courses_data = list(
                student_obj.courses_taken.values(
                    "student_id", "course_id", "semester_taken", "status_krs"
                )
            )

            try:
                run_validators(
                    kode_surat=kode_surat,
                    student_data=student_data,
                    payload=slots,
                    student_courses_data=student_courses_data
                )
            except Exception as validation_exc:
                error_msg = getattr(validation_exc, "detail", None) or str(validation_exc)
                if isinstance(validation_exc, list):
                    error_msg = str(validation_exc[0])
                return {
                    "status": "error",
                    "message": f"❌ **Pengajuan Ditolak oleh Sistem Akademik Kampus:**\n{error_msg}"
                }
            
            # 1. Buat record SuratRequest di PostgreSQL & Picu Background Worker AI Drafting
            surat_req = cls._initialize_surat_request(session_data, user_obj)
            cls._trigger_async_drafting(surat_req.id, session_data)
            
            # 2. Update status sesi jadi selesai / submitted
            session_data["state"] = "COMPLETED"
            AutoSuratSessionManager.save_session(thread_id, session_data)
            
            surat_type_name = surat_req.kode_surat.nama_surat if surat_req.kode_surat else "Surat"
            return {
                "status": "success",
                "message": (
                    f"🚀 **Berhasil!** Permohonan **{surat_type_name}** telah resmi diajukan "
                    f"dengan ID Pengajuan: **#{surat_req.id}**.\n\n"
                    "🤖 Agen AI sedang menyusun draf surat di latar belakang. "
                    "Anda dapat memantau status persetujuan melalui menu riwayat."
                )
            }
        else:
            # JIKA USER BILANG BELUM / ADA YANG SALAH:
            # Reset slot dan destinasi agar tidak terjebak loop is_complete() == True
            session_data["slots"] = {}  
            session_data["sub_state"] = "NORMAL_COLLECTION"
            AutoSuratSessionManager.save_session(thread_id, session_data)
            
            # Ambil label field pertama untuk menagih ulang dari awal
            try:
                surat_type = SuratType.objects.get(kode_surat=session_data.get("intent"))
                required_fields = surat_type.required_payload_fields or []
                first_field_label = required_fields[0].get("label", "data pertama") if required_fields else "data"
            except Exception:
                first_field_label = "data yang diperlukan"

            return {
                "status": "success",
                "message": (
                    "Baik, mari kita ulangi pengisian data dari awal agar semuanya sesuai.\n\n"
                    f"Silakan masukkan kembali **{first_field_label}**:"
                )
            }
            
    @staticmethod
    def _initialize_surat_request(session_data: dict, user_obj) -> SuratRequest:
        kode_surat = session_data.get("intent")
        surat_type = SuratType.objects.filter(kode_surat=kode_surat).first()
        
        slots = session_data.get("slots", {})
        chosen_destination = slots.get("chosen_destination", "email")
        target_destination_address = slots.get("target_destination_address", "")

        try:
            student_obj = user_obj.student_profile
        except AttributeError:
            student_obj = None

        surat_req = SuratRequest.objects.create(
            student=student_obj,
            kode_surat=surat_type,
            status="SUBMITTED",
            payload_data=slots,
            chosen_destination=chosen_destination,
            target_destination_address=target_destination_address,
            user_prompt_context=str(slots)
        )
        return surat_req

    @classmethod
    def _trigger_async_drafting(cls, surat_request_id: int, session_data: dict):
        surat_req = SuratRequest.objects.get(id=surat_request_id)
        dynamic_fields = getattr(surat_req.kode_surat, "required_fields_dict", None)

        thread = threading.Thread(
            target=cls._execute_drafting_worker,
            kwargs={
                "surat_request_id": surat_request_id,
                "user_prompt_context": surat_req.user_prompt_context,
                "kode_surat": surat_req.kode_surat.kode_surat,
                "dynamic_fields_dict": dynamic_fields
            },
            daemon=True
        )
        thread.start()

    @staticmethod
    def _execute_drafting_worker(surat_request_id: int, user_prompt_context: str, kode_surat: str, dynamic_fields_dict: Optional[dict]):
        close_old_connections()
        try:
            agent = SuratDraftingAgent()
            result = agent.draft_letter(
                user_prompt_context=user_prompt_context,
                kode_surat=kode_surat,
                dynamic_fields_dict=dynamic_fields_dict
            )
            
            surat_req = SuratRequest.objects.get(id=surat_request_id)
            surat_req.ai_original_draft = result.get("draft_text", "")
            surat_req.self_critique_score = result.get("score", 0)
            
            if surat_req.self_critique_score >= 8:
                surat_req.status = "PENDING_APPROVAL"
            else:
                surat_req.status = "REVISION_REQUIRED"
                surat_req.revision_notes = result.get("critique", "Perlu penyesuaian data.")
                
            surat_req.save()
            logger.info(f"[Worker] Drafting selesai untuk Surat #{surat_request_id}. Status: {surat_req.status}")
        except Exception as e:
            logger.error(f"[Worker Error] Gagal drafting Surat #{surat_request_id}: {str(e)}\n{traceback.format_exc()}")
            SuratRequest.objects.filter(id=surat_request_id).update(status="FAILED")
    
    @classmethod
    def _handle_chat_review_or_redraft(
        cls,
        thread_id: str,
        user_message: str,
        user_obj,
        session_data: dict
    ) -> Dict[str, Any]:
        """
        Menangani interaksi mahasiswa saat meninjau draf AI (PENDING_APPROVAL / REVISION_REQUIRED):
        - Jika mahasiswa menyetujui draf -> Ubah status SuratRequest ke IN_REVIEW (Kirim ke Dosen).
        - Jika mahasiswa meminta revisi -> Picu Redraft Agent di background dengan catatan revisi user.
        """
        cleaned_msg = user_message.strip().lower()
        
        # 1. Ambil ID SuratRequest aktif dari session atau cari di database
        surat_request_id = session_data.get("active_surat_request_id")
        
        if not surat_request_id:
            try:
                student_obj = user_obj.student_profile
                surat_req = SuratRequest.objects.filter(
                    student=student_obj, 
                    status__in=["PENDING_APPROVAL", "REVISION_REQUIRED", "SUBMITTED"]
                ).latest('created_at')
                surat_request_id = surat_req.id
                session_data["active_surat_request_id"] = surat_request_id
            except SuratRequest.DoesNotExist:
                return {
                    "status": "error",
                    "message": "Tidak ditemukan draf surat aktif yang sedang menunggu review. Silakan buat permohonan baru."
                }
        
        try:
            surat_req = SuratRequest.objects.get(id=surat_request_id)
        except SuratRequest.DoesNotExist:
            return {
                "status": "error",
                "message": "Data permohonan surat tidak ditemukan di database."
            }

        # 2. Cek apakah user menyetujui draf
        approval_keywords = ["setuju", "oke", "ok", "lanjut", "lanjutkan", "kirim", "bagus", "sesuai", "mantap", "ya"]
        is_approved = any(kw in cleaned_msg for kw in approval_keywords)

        if is_approved:
            # Ubah status surat menjadi IN_REVIEW (Kirim ke Gatekeeper Dosen)
            surat_req.status = "IN_REVIEW"
            surat_req.save(update_fields=['status'])
            
            # Selesaikan sesi chat aktif
            session_data["state"] = "IN_REVIEW"
            session_data["sub_state"] = "IN_REVIEW"
            AutoSuratSessionManager.save_session(thread_id, session_data)
            
            surat_name = surat_req.kode_surat.nama_surat if surat_req.kode_surat else "Surat"
            return {
                "status": "success",
                "message": (
                    f"✅ **Draf disetujui!** Permohonan **{surat_name}** "
                    f"telah resmi dikirim ke dosen pembimbing / bagian akademik untuk ditinjau (**IN_REVIEW**).\n\n"
                    "Silakan pantau status persetujuan surat melalui menu riwayat."
                )
            }
        else:
            # 3. Jika user memberikan catatan revisi (Redraft)
            surat_req.status = "REVISION_REQUIRED"
            surat_req.revision_notes = user_message
            surat_req.save(update_fields=['status', 'revision_notes'])
            
            # Picu background worker untuk merombak draf
            cls._trigger_async_redrafting(surat_req.id, user_message)
            
            return {
                "status": "success",
                "message": (
                    "🔄 **Catatan revisi diterima.** Agen AI sedang menyesuaikan dan merombak "
                    "draf surat berdasarkan instruksimu di latar belakang. Mohon tunggu sebentar..."
                )
            }

    @classmethod
    def _trigger_async_redrafting(cls, surat_request_id: int, revision_notes: str):
        """Menjalankan proses redrafting AI di background thread terpisah."""
        thread = threading.Thread(
            target=cls._execute_redrafting_worker,
            kwargs={
                "surat_request_id": surat_request_id,
                "revision_notes": revision_notes
            },
            daemon=True
        )
        thread.start()
    @staticmethod
    def _execute_redrafting_worker(surat_request_id: int, revision_notes: str):
        """Worker background khusus untuk merombak struktur/redaksi surat berdasarkan catatan revisi user."""
        close_old_connections()
        try:
            surat_req = SuratRequest.objects.get(id=surat_request_id)
            agent = SuratDraftingAgent()
            
            # Panggil method khusus redraft / revisi dengan membawa draf lama & catatan revisi
            # (Pastikan di SuratDraftingAgent kamu memiliki method redraft atau sesuaikan namanya)
            result = agent.redraft_letter(
                existing_draft=surat_req.ai_original_draft,
                payload_data=surat_req.payload_data,
                revision_notes=revision_notes,
                kode_surat=surat_req.kode_surat.kode_surat
            )
            
            # Perbarui draf dengan hasil revisi struktur yang baru
            surat_req.ai_original_draft = result.get("draft_text", surat_req.ai_original_draft)
            surat_req.self_critique_score = result.get("score", surat_req.self_critique_score)
            surat_req.status = "PENDING_APPROVAL"  # Kembalikan ke mahasiswa untuk direview ulang
            surat_req.save()
            
            logger.info(f"[Worker] Redrafting struktur selesai untuk Surat #{surat_request_id}.")
        except Exception as e:
            logger.error(f"[Worker Error] Gagal redrafting Surat #{surat_request_id}: {str(e)}")
            SuratRequest.objects.filter(id=surat_request_id).update(status="REVISION_FAILED")