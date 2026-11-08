import json
import logging
import copy
from typing import Dict, Any, Optional
from django.core.cache import cache
from surat.models import ChatThread, ChatMessage
from surat.services.surat_orchestrator import SuratWorkflowOrchestrator

logger = logging.getLogger(__name__)

class AutoSuratSessionManager:
    TTL_SECONDS = 1800  # 30 Menit Sliding TTL

    @staticmethod
    def _get_redis_key(thread_id: str) -> str:
        return f"autosurat:session:{thread_id}"

    @classmethod
    def initialize_thread(cls, user_obj, initial_title: str = "Percakapan Baru") -> ChatThread:
        """
        Fungsi eksplisit untuk endpoint /chat/init/ (atau tombol New Chat).
        Membuat thread baru di PostgreSQL dan mendaftarkan state awalnya ke Redis.
        """
        thread = ChatThread.objects.create(
            user=user_obj,
            title=initial_title,
            status=ChatThread.ThreadStatus.ACTIVE
        )
        
        initial_session_data = {
            "state": "ACTIVE",
            "intent": None,
            "pending_intent": None,
            "sub_state": "NORMAL_COLLECTION",
            "is_waiting_for_switch_confirmation": False,
            "slots": {},
            "missing_slots": []
        }
        
        redis_key = cls._get_redis_key(str(thread.id))
        cache.set(redis_key, initial_session_data, timeout=cls.TTL_SECONDS)
        
        return thread

    @classmethod
    def get_session(cls, thread_id: str, user_obj=None) -> Dict[str, Any]:
        """Mengambil state aktif dari Redis, sekaligus verifikasi lazy-expiration database."""
        redis_key = cls._get_redis_key(thread_id)
        session_data = cache.get(redis_key)

        if not session_data:
            thread_query = ChatThread.objects.filter(id=thread_id)
            if user_obj:
                thread_query = thread_query.filter(user=user_obj)
            
            thread = thread_query.first()
            
            if thread:
                if thread.status == ChatThread.ThreadStatus.ACTIVE:
                    thread.status = ChatThread.ThreadStatus.EXPIRED
                    thread.save(update_fields=['status', 'updated_at'])
                
                return {
                    "state": thread.status,
                    "intent": None,
                    "pending_intent": None,
                    "sub_state": "LOCKED",
                    "is_waiting_for_switch_confirmation": False,
                    "slots": {}
                }
            
            return {
                "state": "NOT_FOUND",
                "sub_state": "LOCKED",
                "slots": {}
            }

        return session_data
    
    @classmethod
    def save_session(
        cls, 
        thread_id: str, 
        state: str, 
        sub_state: Optional[str] = None,
        intent: Optional[str] = None, 
        slots: Optional[Dict[str, Any]] = None, 
        missing_slots: Optional[list] = None,
        pending_intent: Optional[str] = None,
        is_waiting_for_switch_confirmation: Optional[bool] = None
    ):
        """Menyimpan atau memperbarui state ke Redis Cache dengan Sliding TTL 30 menit."""
        redis_key = cls._get_redis_key(thread_id)
        existing = cache.get(redis_key) or {}
        
        updated_data = {
            "state": state,
            "intent": intent if intent is not None else existing.get("intent"),
            "sub_state": sub_state if sub_state is not None else existing.get("sub_state", "NORMAL_COLLECTION"), 
            "pending_intent": pending_intent if pending_intent is not None else existing.get("pending_intent"),
            "is_waiting_for_switch_confirmation": is_waiting_for_switch_confirmation if is_waiting_for_switch_confirmation is not None else existing.get("is_waiting_for_switch_confirmation", False),
            "slots": slots if slots is not None else existing.get("slots", {}),
            "missing_slots": missing_slots if missing_slots is not None else existing.get("missing_slots", [])
        }

        cache.set(redis_key, updated_data, timeout=cls.TTL_SECONDS)

    @classmethod
    def clear_session(cls, thread_id: str, target_status: str = ChatThread.ThreadStatus.CANCELLED):
        """Menghapus cache Redis dan mensinkronisasi status akhir ke PostgreSQL."""
        redis_key = cls._get_redis_key(thread_id)
        cache.delete(redis_key)
        ChatThread.objects.filter(id=thread_id).update(status=target_status)

    @classmethod
    def handle_incoming_message(cls, thread_id: Optional[str], user_message: str, user_obj) -> tuple[str, str]:
        """
        Handler utama dengan dukungan Lazy Initialization & Pengamanan Sesi Expired/Cancelled:
        1. Menangani Lazy Initialization jika thread_id kosong/null/baru.
        2. Cek status sesi (ACTIVE, EXPIRED, CANCELLED, NOT_FOUND).
        3. Interceptor Keyword Pembatalan Global.
        4. Pencatatan riwayat & eksekusi orkestrator.
        
        Returns:
            tuple: (reply_message, active_thread_id)
        """
        cleaned_msg = user_message.strip()
        cleaned_msg_lower = cleaned_msg.lower()

        # --- FITUR LAZY INITIALIZATION ---
        if not thread_id:
            new_thread = cls.initialize_thread(user_obj, initial_title=cleaned_msg[:30] + "...")
            thread_id = str(new_thread.id)

        session_data = cls.get_session(thread_id, user_obj)
        current_state = session_data.get("state")

        # --- PENGAWASAN SESI EXPIRED / CANCELLED / NOT_FOUND ---
        if current_state in ["EXPIRED", "CANCELLED", "NOT_FOUND"]:
            if current_state == "EXPIRED":
                msg = "[SESSION_EXPIRED] Sesi Anda telah kedaluwarsa karena tidak ada aktivitas selama 30 menit. Silakan buat percakapan baru (New Chat) untuk melanjutkan."
            elif current_state == "CANCELLED":
                msg = "[SESSION_CANCELLED] Sesi permohonan surat ini telah dibatalkan. Silakan buat percakapan baru (New Chat) jika ingin mulai kembali."
            else:
                msg = "[THREAD_NOT_FOUND] Sesi percakapan tidak ditemukan. Silakan buat percakapan baru."
            
            return msg, thread_id

        # 1. Interceptor Keyword Pembatalan Global
        cancellation_keywords = ["batal", "reset", "cancel", "mulai ulang", "stop", "hentikan", "hapus"]
        if cleaned_msg_lower in cancellation_keywords:
            cls.clear_session(thread_id, target_status=ChatThread.ThreadStatus.CANCELLED)
            ChatThread.objects.filter(id=thread_id).update(
                document_type=None, 
                title="Permohonan Surat Dibatalkan"
            )
            return "[SESSION_CANCELLED] Sesi permohonan surat ini telah dibatalkan. Silakan buat percakapan baru (New Chat) jika ingin mulai kembali.", thread_id

        # 2. Log pesan masuk ke PostgreSQL
        thread = ChatThread.objects.filter(id=thread_id, user=user_obj).first()
        if thread:
            ChatMessage.objects.create(
                thread=thread,
                sender='user',
                message=user_message,
                slots_snapshot=copy.deepcopy(session_data.get("slots", {}))
            )

        # 3. Teruskan ke orkestrator logika alur chat
        orchestrator_result = SuratWorkflowOrchestrator.process_incoming_chat(thread_id, cleaned_msg, user_obj)
        reply_message = orchestrator_result.get("message", "")
        
        # 4. Simpan kembali pembaruan state session_data ke Redis Cache (Sliding TTL)
        cache.set(cls._get_redis_key(thread_id), session_data, timeout=cls.TTL_SECONDS)

        return reply_message, thread_id