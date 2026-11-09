import json
import logging
from typing import Dict, Any, Optional
from django.core.cache import cache
from surat.models import ChatThread

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
            "sub_state": "WAITING_FOR_INTENT",  # Diselaraskan dengan awal orkestrator
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