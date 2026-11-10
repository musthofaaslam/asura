import logging
import threading
from typing import Any, Dict, Optional

from django.db import close_old_connections
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import CustomJWTAuthentication
from accounts.models import Lecturer, Student
from accounts.serializers import StandardErrorResponseSerializer

from .agents.execution_tools import (
    ExecutionToolError,
    SuratExecutionTools,
)
from .agents.letter_agent import (
    SuratAgentError,
    SuratAgentRateLimitError,
    SuratAgentTimeoutError,
    SuratDraftingAgent,
)
from .agents.memory_agent import (
    AgenticMemoryAgent,
    MemoryAgentError,
)
from .models import (
    ApprovalStatus,
    SuratApproval,
    SuratAuditLog,
    SuratRequest,
    SuratStatus,
    SuratType,
    TteSignature,
    ChatThread,
    ChatMessage,
)
from .serializers import (
    ApprovalActionErrorResponseSerializer,
    LecturerApprovalActionRequestSerializer,
    LecturerApprovalActionResponseSerializer,
    SuratApprovalActionSerializer,
    SuratApprovalSerializer,
    SuratRejectActionSerializer,
    SuratRequestCreateSerializer,
    SuratRequestDetailSerializer,
    SuratRequestListSerializer,
    SuratRequestSuccessResponseSerializer,
    SuratTypeSerializer,
    SuratVerifyInvalidResponseSerializer,
    SuratVerifyValidResponseSerializer,
    ReDraftSuratRequestSerializer,
)
from .services.workflow_service import process_approval_action
from .services.manual_handlers import create_approval_workflow_for_request
from .services.session_manager import AutoSuratSessionManager

logger = logging.getLogger(__name__)

def _mark_surat_request_failed(
    surat_request_id: int,
    error_code: str,
    error_reason: str,
) -> None:
    """
    Memperbarui status SuratRequest menjadi FAILED dan mencatat alasan kegagalan
    apabila eksekusi agen atau pipeline di latar belakang mengalami kendala.
    """
    try:
        surat_req = SuratRequest.objects.filter(pk=surat_request_id).first()
        if surat_req is not None:
            existing_notes = (
                list(surat_req.revision_notes)
                if isinstance(surat_req.revision_notes, list)
                else []
            )
            existing_notes.append(error_reason)
            surat_req.status = SuratStatus.FAILED
            surat_req.revision_notes = existing_notes
            surat_req.save(update_fields=["status", "revision_notes"])

            SuratAuditLog.objects.create(
                surat_request=surat_req,
                action_id=error_code[:100],
                action=f"AI_DRAFT_FAILED: {error_reason}"[:255],
            )
    except Exception as db_exc:
        logger.error(
            "Gagal memperbarui status FAILED untuk SuratRequest id=%s: %s",
            surat_request_id,
            db_exc,
        )

# def _execute_drafting_agent_worker(
#     surat_request_id: int,
#     user_prompt_context: str,
#     kode_surat: str,
#     student_metadata: Optional[Dict[str, Any]] = None,
#     agent: Optional[SuratDraftingAgent] = None,
#     dynamic_fields_dict: Optional[Dict[str, Any]] = None
# ) -> None:
#     """
#     Worker asinkron untuk menjalankan SuratDraftingAgent.draft_letter() di latar belakang.
#     Menyimpan hasil ekstraksi Pydantic ke kolom payload_data, menyimpan draf asli AI serta
#     skor evaluasi diri, lalu mengunci status pada PENDING_APPROVAL (State Pausing).
#     """
#     close_old_connections()
#     try:
#         active_agent = agent if agent is not None else SuratDraftingAgent()
#         draft_output = active_agent.draft_letter(
#             user_prompt_context=user_prompt_context,
#             kode_surat=kode_surat,
#             student_metadata=student_metadata,
#             dynamic_fields=dynamic_fields_dict
#         )

#         # CARA AMAN MENGEKSTRAK PAYLOAD DARI AGEN
#         if hasattr(draft_output, "to_payload_dict") and callable(draft_output.to_payload_dict):
#             structured_payload: Dict[str, Any] = draft_output.to_payload_dict()
#         elif hasattr(draft_output, "extracted_payload"):
#             # Jika menggunakan objek Pydantic dinamis standar dari create_dynamic_agent_output_schema
#             payload_obj = draft_output.extracted_payload
#             if hasattr(payload_obj, "model_dump") and callable(payload_obj.model_dump):
#                 structured_payload = payload_obj.model_dump(mode="json")
#             else:
#                 structured_payload = dict(payload_obj)
#         elif hasattr(draft_output, "model_dump") and callable(draft_output.model_dump):
#             structured_payload = draft_output.model_dump(mode="json")
#         else:
#             structured_payload = dict(draft_output)

#         surat_req = SuratRequest.objects.get(pk=surat_request_id)
#         surat_req.payload_data = structured_payload
#         surat_req.ai_original_draft = draft_output.ai_original_draft
#         surat_req.self_critique_score = draft_output.evaluation.self_critique_score
#         surat_req.revision_notes = list(draft_output.evaluation.revision_notes)
        
#         critique_score = draft_output.evaluation.self_critique_score
        
#         # Pengecekan has_missing_info dihapus karena slot data sudah divalidasi tuntas di sesi chat.
#         # Kita hanya menjaga ambang batas kualitas redaksi AI (misal skor di bawah 7 memerlukan tinjauan ulang).
#         if critique_score < 7:
#             surat_req.status = SuratStatus.REVISION_REQUIRED
#             surat_req.save(
#                 update_fields=[
#                     "payload_data",
#                     "ai_original_draft",
#                     "self_critique_score",
#                     "revision_notes",
#                     "status",
#                 ]
#             )
            
#             SuratAuditLog.objects.create(
#                 surat_request=surat_req,
#                 action_id="AI_REV_REQ",
#                 action="AI_DRAFTING_LOW_QUALITY_SCORE",
#             )
#             logger.warning(
#                 "SuratRequest id=%s dikembalikan karena skor evaluasi diri AI terlalu rendah (Skor: %s).",
#                 surat_request_id,
#                 critique_score,
#             )
#             return

#         # STATE PAUSING: Ubah status ke PENDING_APPROVAL dan hentikan alur eksekusi.
#         # DILARANG memanggil fungsi pembuatan Nomor Surat, TTE, QR Code, atau Cloudinary.
#         surat_req.status = SuratStatus.PENDING_APPROVAL
#         surat_req.save(
#             update_fields=[
#                 "payload_data",
#                 "ai_original_draft",
#                 "self_critique_score",
#                 "revision_notes",
#                 "status",
#             ]
#         )

#         create_approval_workflow_for_request(surat_req, surat_req.student)
        
#         SuratAuditLog.objects.create(
#             surat_request=surat_req,
#             action_id="AI_02",
#             action="AI_DRAFT_COMPLETED_PENDING_APPROVAL",
#         )
#         logger.info(
#             "Penyusunan draf surat selesai untuk SuratRequest id=%s (status=%s, skor=%s).",
#             surat_request_id,
#             surat_req.status,
#             surat_req.self_critique_score,
#         )
#     except (SuratAgentTimeoutError, SuratAgentRateLimitError) as exc:
#         logger.error(
#             "Kegagalan Timeout/RateLimit LLM pada SuratRequest id=%s (%s): %s",
#             surat_request_id,
#             type(exc).__name__,
#             exc,
#         )
#         _mark_surat_request_failed(
#             surat_request_id=surat_request_id,
#             error_code="AI_ERR_LIMIT",
#             error_reason=f"{type(exc).__name__}: {exc}",
#         )
#     except SuratAgentError as exc:
#         logger.error(
#             "Kesalahan operasional SuratDraftingAgent pada SuratRequest id=%s (%s): %s",
#             surat_request_id,
#             type(exc).__name__,
#             exc,
#         )
#         _mark_surat_request_failed(
#             surat_request_id=surat_request_id,
#             error_code="AI_ERR_AGENT",
#             error_reason=f"{type(exc).__name__}: {exc}",
#         )
#     except Exception as exc:
#         logger.exception(
#             "Kesalahan tak terduga saat menyusun draf surat pada SuratRequest id=%s: %s",
#             surat_request_id,
#             exc,
#         )
#         _mark_surat_request_failed(
#             surat_request_id=surat_request_id,
#             error_code="AI_ERR_UNEXPECTED",
#             error_reason=f"{type(exc).__name__}: {exc}",
#         )
#     finally:
#         close_old_connections()


# def _execute_redraft_agent_worker(
#     surat_request_id: int,
#     user_prompt_context: str,
#     kode_surat: str,
#     previous_draft: str,
#     previous_payload: dict,
#     previous_score: int,
#     revision_notes: list,
#     student_metadata: dict
# ) -> None:
#     """
#     Worker asinkron untuk memproses ulang penyusunan draf surat (Self-Reflection Re-draft)
#     ketika pengguna memberikan informasi tambahan berdasarkan revision_notes.
#     """
#     close_old_connections()
#     try:
#         surat_req = SuratRequest.objects.get(pk=surat_request_id)
        
#         active_agent = SuratDraftingAgent()
#         # Memanggil fungsi revisi iteratif agen
#         draft_output = active_agent.draft_letter_with_reflection(
#             user_prompt_context=user_prompt_context,
#             kode_surat=kode_surat,
#             previous_draft=previous_draft,
#             previous_payload=previous_payload,
#             previous_score=previous_score,
#             revision_notes=revision_notes,
#             student_metadata=student_metadata,
#         )

#         if hasattr(draft_output, "to_payload_dict") and callable(draft_output.to_payload_dict):
#             structured_payload: Dict[str, Any] = draft_output.to_payload_dict()
#         elif hasattr(draft_output, "extracted_payload"):
#             # Jika menggunakan objek Pydantic dinamis standar dari create_dynamic_agent_output_schema
#             payload_obj = draft_output.extracted_payload
#             if hasattr(payload_obj, "model_dump") and callable(payload_obj.model_dump):
#                 structured_payload = payload_obj.model_dump(mode="json")
#             else:
#                 structured_payload = dict(payload_obj)
#         elif hasattr(draft_output, "model_dump") and callable(draft_output.model_dump):
#             structured_payload = draft_output.model_dump(mode="json")
#         else:
#             structured_payload = dict(draft_output)


#         critique_score = draft_output.evaluation.self_critique_score
#         # Memeriksa apakah masih ada field opsional penting yang bernilai kosong/null
#         has_null_required = any(
#             v is None for k, v in structured_payload.items() 
#             if k in ["tanggal_mulai", "tanggal_selesai", "posisi_dilamar"]
#         )

#         surat_req.payload_data = structured_payload
#         surat_req.ai_original_draft = draft_output.ai_original_draft
#         surat_req.self_critique_score = critique_score
#         surat_req.revision_notes = list(draft_output.evaluation.revision_notes)

#         if critique_score < 8 or has_null_required:
#             surat_req.status = SuratStatus.REVISION_REQUIRED
#             action_code = "AI_REDRAFT_STILL_INCOMPLETE"
#         else:
#             surat_req.status = SuratStatus.PENDING_APPROVAL
#             action_code = "AI_REDRAFT_SUCCESS_PENDING_APPROVAL"

#         surat_req.save(
#             update_fields=[
#                 "payload_data",
#                 "ai_original_draft",
#                 "self_critique_score",
#                 "revision_notes",
#                 "status",
#             ]
#         )

#         SuratAuditLog.objects.create(
#             surat_request=surat_req,
#             action_id="AI_02",
#             action=action_code,
#         )
#         logger.info(
#             "Re-draft surat selesai untuk SuratRequest id=%s (status=%s, skor=%s).",
#             surat_request_id,
#             surat_req.status,
#             surat_req.self_critique_score,
#         )
#     except Exception as exc:
#         logger.exception(
#             "Kesalahan saat re-draft surat pada SuratRequest id=%s: %s",
#             surat_request_id,
#             exc,
#         )
#         _mark_surat_request_failed(
#             surat_request_id=surat_request_id,
#             error_code="AI_REDRAFT_ERR",
#             error_reason=f"{type(exc).__name__}: {exc}",
#         )
#     finally:
#         close_old_connections()


        
# class ChatSessionMessageAPIView(APIView):
#     """
#     Endpoint untuk memproses pesan obrolan mahasiswa secara interaktif.
#     Menggunakan AutoSuratSessionManager (Redis) + Chat Workflow Service 
#     untuk Intent Detection (Lapis 1 & 2), State Locking, dan Accumulative Slot Filling.
#     POST /api/surat/chat/<uuid:thread_id>/message/
#     """
#     permission_classes = [permissions.IsAuthenticated]
#     authentication_classes = [CustomJWTAuthentication]

#     def post(self, request, thread_id):
#         user_msg = request.data.get("message", "").strip()
#         if not user_msg:
#             return Response(
#                 {"status": "error", "message": "Pesan tidak boleh kosong."},
#                 status=status.HTTP_400_BAD_REQUEST
#             )

#         # 1. Pastikan thread milik user yang sedang login
#         thread = ChatThread.objects.filter(id=thread_id, user=request.user).first()
#         if not thread:
#             return Response(
#                 {"status": "error", "message": "Sesi chat atau thread tidak ditemukan."},
#                 status=status.HTTP_404_NOT_FOUND
#             )

#         # 2. Jalankan AutoSuratSessionManager untuk menangani pesan masuk, 
#         # pembatalan global (pilar 5), TTL Redis, dan pencatatan riwayat ke PostgreSQL (ChatMessage).
#         reply_message = AutoSuratSessionManager.handle_incoming_message(
#             thread_id=str(thread.id),
#             user_message=user_msg,
#             user_obj=request.user
#         )

#         # 3. Ambil kembali state terbaru dari Redis setelah diproses
#         session_data = AutoSuratSessionManager.get_session(str(thread.id))
        
#         if session_data.get("state") in ["CANCELLED", "EXPIRED"]:
#             return Response({
#                 "status": "success",
#                 "state": session_data["state"],
#                 "reply": reply_message
#             }, status=status.HTTP_200_OK)

#         intent = session_data.get("intent") # Kode surat aktif (misal: "SI-MAG")
#         slots = session_data.get("slots", {})
#         waiting_confirmation = session_data.get("is_waiting_for_switch_confirmation", False)

#         # Ambil daftar required_fields dari database Supabase (SuratType) berdasarkan intent aktif
#         required_fields = []
#         if intent:
#             try:
#                 surat_type_obj = SuratType.objects.get(kode_surat=intent)
#                 required_fields = surat_type_obj.required_payload_fields or []
#             except SuratType.DoesNotExist:
#                 pass

#         # 4. Jika semua required fields sudah lengkap, berikan sinyal ke frontend 
#         # bahwa sesi siap diconvert menjadi SuratRequest (Fase Drafting Agent).
#         surat_is_complete = False
#         if intent and required_fields:
#             surat_is_complete = all(
#                 item.get("field") in slots and bool(slots[item.get("field")])
#                 for item in required_fields
#             )

#         if surat_is_complete:
#             return Response({
#                 "status": "ready_to_draft",
#                 "state": "READY_TO_DRAFT",
#                 "kode_surat": intent,
#                 "collected_slots": slots,
#                 "required_fields": required_fields,
#                 "reply": reply_message
#             }, status=status.HTTP_200_OK)

#         # 5. Kembalikan balasan interaktif normal ke frontend (menampilkan checklist / tagihan field berikutnya)
#         return Response({
#             "status": "success",
#             "state": "COLLECTING_SLOTS",
#             "detected_intent": intent,
#             "is_waiting_for_switch_confirmation": waiting_confirmation,
#             "reply": reply_message,
#             "collected_slots": slots,
#             "required_fields": required_fields  # <-- Ditambahkan agar frontend langsung mendapat kontrak field wajib
#         }, status=status.HTTP_200_OK)
                        
# class SuratRequestRedraftAPIView(APIView):
#     """
#     Endpoint bagi mahasiswa untuk memberikan informasi tambahan / revisi 
#     pada surat yang berstatus REVISION_REQUIRED.
#     PATCH /api/surat/requests/<int:pk>/re-draft/
#     """
#     permission_classes = [permissions.IsAuthenticated]
#     authentication_classes = [CustomJWTAuthentication]
#     parser_classes = [JSONParser, FormParser, MultiPartParser]
#     _last_worker_thread: Optional[threading.Thread] = None

#     @extend_schema(
#         summary="Perbarui Draf Surat Berdasarkan Catatan Revisi (Mahasiswa)",
#         description=(
#             "Digunakan mahasiswa untuk mengirimkan informasi tambahan guna melengkapi "
#             "draf surat yang sebelumnya berstatus REVISION_REQUIRED. "
#             "Agen AI akan memproses ulang draf dengan menggabungkan konteks lama dan koreksi baru."
#         ),
#         request=ReDraftSuratRequestSerializer,
#         responses={
#             202: SuratRequestSuccessResponseSerializer,
#             400: StandardErrorResponseSerializer,
#             404: StandardErrorResponseSerializer,
#         },
#     )
#     def post(self, request):
#         try:
#             student_obj = request.user.student_profile
#         except (Student.DoesNotExist, AttributeError):
#             return Response(
#                 {
#                     "status": "error",
#                     "message": "Validasi gagal",
#                     "errors": "Bukan akun mahasiswa. Hanya mahasiswa yang dapat mengajukan surat.",
#                 },
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         serializer = SuratRequestCreateSerializer(
#             data=request.data,
#             context={"request": request},
#         )
#         if not serializer.is_valid():
#             return Response(
#                 {
#                     "status": "error",
#                     "message": "Validasi gagal",
#                     "errors": serializer.errors,
#                 },
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         kode_surat_str = serializer.validated_data["kode_surat"]
#         try:
#             surat_type = SuratType.objects.get(kode_surat=kode_surat_str)
#         except SuratType.DoesNotExist:
#             return Response(
#                 {
#                     "status": "error",
#                     "message": "Validasi gagal",
#                     "errors": "Jenis surat tidak ditemukan.",
#                 },
#                 status=status.HTTP_404_NOT_FOUND,
#             )

#         # =========================================================================
#         # PENCEGAHAN DOUBLE REQUEST:
#         # Cek apakah mahasiswa ini masih memiliki permohonan aktif untuk kode_surat yang sama.
#         # Status aktif meliputi: Sedang diproses AI, Menunggu Dosen, atau Perlu Revisi.
#         # =========================================================================
#         active_statuses = [
#             SuratStatus.SUBMITTED,
#             SuratStatus.PENDING_APPROVAL,
#             SuratStatus.REVISION_REQUIRED,
#         ]
#         existing_active_request = SuratRequest.objects.filter(
#             student=student_obj,
#             kode_surat=surat_type,
#             status__in=active_statuses
#         ).first()

#         if existing_active_request:
#             return Response(
#                 {
#                     "status": "error",
#                     "message": "Pengajuan Ganda Ditolak",
#                     "errors": (
#                         f"Anda masih memiliki permohonan aktif untuk jenis surat '{surat_type.nama_surat}' "
#                         f"dengan status '{existing_active_request.status}'. "
#                         "Silakan selesaikan atau periksa permohonan yang sudah ada."
#                     ),
#                     "existing_request_id": existing_active_request.id,
#                     "surat_status": existing_active_request.status,
#                 },
#                 status=status.HTTP_400_BAD_REQUEST,
#             )
#         # =========================================================================

#         user_prompt_context = serializer.validated_data.get("user_prompt_context", "")
#         initial_payload = serializer.validated_data.get("payload_data") or {}

#         student_metadata = {
#             "nim": student_obj.nim,
#             "nama": getattr(student_obj.user, "nama", ""),
#             "prodi": getattr(student_obj.prodi, "nama_prodi", ""),
#             "fakultas": getattr(student_obj.fakultas, "nama_fakultas", ""),
#             "semester_aktif": student_obj.semester_aktif,
#             "tahun_akademik": student_obj.tahun_akademik,
#         }

#         # Buat rekam data awal dengan status SUBMITTED
#         surat_req = SuratRequest.objects.create(
#             student=student_obj,
#             kode_surat=surat_type,
#             status=SuratStatus.SUBMITTED,
#             payload_data=initial_payload,
#             user_prompt_context=user_prompt_context,
#         )

#         SuratAuditLog.objects.create(
#             surat_request=surat_req,
#             action_id="AI_01",
#             action="SURAT_SUBMITTED_FOR_AI_DRAFTING",
#         )

#         dynamic_fields_dict = getattr(surat_type, "required_fields_dict", None)

#         worker_thread = threading.Thread(
#             target=_execute_drafting_agent_worker,
#             kwargs={
#                 "surat_request_id": surat_req.id,
#                 "user_prompt_context": user_prompt_context,
#                 "kode_surat": surat_type.kode_surat,
#                 "student_metadata": student_metadata,
#                 "agent": self.get_drafting_agent(),
#                 "dynamic_fields_dict": dynamic_fields_dict,
#             },
#             name=f"surat-drafting-agent-{surat_req.id}",
#             daemon=True,
#         )
#         SuratRequestCreateAPIView._last_worker_thread = worker_thread
#         worker_thread.start()

#         return Response(
#             {
#                 "status": "success",
#                 "message": (
#                     f"Permohonan surat {surat_type.nama_surat} berhasil diterima dan sedang "
#                     "diproses secara asinkron oleh agen AI."
#                 ),
#                 "request_id": surat_req.id,
#                 "surat_status": surat_req.status,
#                 "data": {
#                     "request_id": surat_req.id,
#                     "kode_surat": surat_type.kode_surat,
#                     "status": surat_req.status,
#           },
#             },
#             status=status.HTTP_202_ACCEPTED,
#         )

# class SuratRequestCreateAPIView(APIView):
#     """
#     Endpoint bagi mahasiswa untuk mengajukan surat baru berbasis Agentic AI (Fase 1).
#     Menerima kode_surat serta konteks permohonan, membuat record awal dengan status SUBMITTED,
#     dan menjalankan SuratDraftingAgent.draft_letter() secara asinkron di latar belakang
#     hingga mencapai status PENDING_APPROVAL (State Pausing).
#     POST /api/surat/request/
#     """
#     permission_classes = [permissions.IsAuthenticated]
#     authentication_classes = [CustomJWTAuthentication]
#     parser_classes = [MultiPartParser, FormParser, JSONParser]
#     agent_instance: Optional[SuratDraftingAgent] = None
#     _last_worker_thread: Optional[threading.Thread] = None

#     def get_drafting_agent(self) -> Optional[SuratDraftingAgent]:
#         """
#         Mengembalikan instance SuratDraftingAgent kustom apabila disuntikkan (misal pada pengujian),
#         atau None agar worker menginisialisasi instance baru di dalam background thread.
#         """
#         return self.agent_instance

#     @extend_schema(
#         summary="Ajukan Surat Baru (Mahasiswa)",
#         description=(
#             "Digunakan mahasiswa untuk mengajukan permohonan pembuatan surat. "
#             "Mendukung pengiriman konteks bahasa alami (user_prompt_context / konteks) "
#             "dan payload dinamis. Pembuatan draf dieksekusi secara asinkron di latar belakang "
#             "oleh SuratDraftingAgent dan berhenti pada status PENDING_APPROVAL (State Pausing)."
#         ),
#         request=SuratRequestCreateSerializer,
#         responses={
#             201: SuratRequestSuccessResponseSerializer,
#             202: SuratRequestSuccessResponseSerializer,
#             400: StandardErrorResponseSerializer,
#             403: StandardErrorResponseSerializer,
#             404: StandardErrorResponseSerializer,
#         },
#     )
#     def post(self, request):
#         try:
#             student_obj = request.user.student_profile
#         except (Student.DoesNotExist, AttributeError):
#             return Response(
#                 {
#                     "status": "error",
#                     "message": "Validasi gagal",
#                     "errors": "bukan akun mahasiswa. Hanya mahasiswa yang dapat mengajukan surat.",
#                 },
#                 status=status.HTTP_400_BAD_REQUEST,
#             )
#         # ndwfevirc kwc
#         raw_kode_surat = request.data.get("kode_surat")
#         if isinstance(raw_kode_surat, str) and raw_kode_surat.strip():
#             try:
#                 surat_type = SuratType.objects.get(kode_surat=raw_kode_surat.strip())
#             except SuratType.DoesNotExist:
#                 return Response(
#                     {
#                         "status": "error",
#                         "message": "Validasi gagal",
#                         "errors": "Jenis surat tidak ditemukan.",
#                     },
#                     status=status.HTTP_404_NOT_FOUND,
#                 )
#         else:
#             surat_type = None

#         serializer = SuratRequestCreateSerializer(
#             data=request.data,
#             context={"request": request},
#         )
#         if not serializer.is_valid():
#             return Response(
#                 {
#                     "status": "error",
#                     "message": "Validasi gagal",
#                     "errors": serializer.errors,
#                 },
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         kode_surat_str = serializer.validated_data["kode_surat"]
#         if surat_type is None:
#             try:
#                 surat_type = SuratType.objects.get(kode_surat=kode_surat_str)
#             except SuratType.DoesNotExist:
#                 return Response(
#                     {
#                         "status": "error",
#                         "message": "Validasi gagal",
#                         "errors": "Jenis surat tidak ditemukan.",
#                     },
#                     status=status.HTTP_404_NOT_FOUND,
#                 )

#         user_prompt_context = serializer.validated_data.get("user_prompt_context", "")
#         initial_payload = serializer.validated_data.get("payload_data") or {}

#         student_metadata = {
#                     "nim": student_obj.nim,
#                     "nama": getattr(student_obj.user, "nama", ""),
#                     "prodi": getattr(student_obj.prodi, "nama_prodi", ""),
#                     "fakultas": getattr(student_obj.fakultas, "nama_fakultas", ""),
#                     "semester_aktif": student_obj.semester_aktif,
#                     "tahun_akademik": student_obj.tahun_akademik,
#                 }
        
#         agent = self.get_drafting_agent() or SuratDraftingAgent()
#         # State Pausing: Buat rekam data awal dengan status SUBMITTED tanpa memicu
#         # pembuatan Nomor Surat, TTE, QR Code, atau unggahan Cloudinary.
#         chosen_destination = serializer.validated_data.get("chosen_destination") or request.data.get("chosen_destination")
#         target_destination_address = serializer.validated_data.get("target_destination_address") or request.data.get("target_destination_address")
#         # State Pausing: Buat rekam data awal dengan status SUBMITTED serta simpan preferensi tujuannya
#         surat_req = SuratRequest.objects.create(
#             student=student_obj,
#             kode_surat=surat_type,
#             status=SuratStatus.SUBMITTED,
#             payload_data=initial_payload,
#             user_prompt_context=user_prompt_context,
#             chosen_destination=chosen_destination,             # <-- Tambahkan ini
#             target_destination_address=target_destination_address, # <-- Tambahkan ini (sesuaikan nama field modelmu)
#         )

#         SuratAuditLog.objects.create(
#             surat_request=surat_req,
#             action_id="AI_01",
#             action="SURAT_SUBMITTED_FOR_AI_DRAFTING",
#         )

#         # Ambil field dinamis yang tersimpan di kolom/atribut jenis surat (sesuaikan nama field modelnya, misal: required_fields_dict)
#         dynamic_fields_dict = getattr(surat_type, "required_fields_dict", None)

#         first_approval = SuratApproval.objects.filter(surat_request=surat_req).order_by('step_order').first()
#         approval_id = first_approval.id if first_approval else None
        
#         worker_thread = threading.Thread(
#             target=_execute_drafting_agent_worker,
#             kwargs={
#                 "surat_request_id": surat_req.id,
#                 "user_prompt_context": user_prompt_context,
#                 "kode_surat": surat_type.kode_surat,
#                 "student_metadata": student_metadata,
#                 "agent": self.get_drafting_agent(),
#                 "dynamic_fields_dict": dynamic_fields_dict, # <-- Teruskan data dinamis ke worker
#             },
#             name=f"surat-drafting-agent-{surat_req.id}",
#             daemon=True,
#         )
#         SuratRequestCreateAPIView._last_worker_thread = worker_thread
#         worker_thread.start()

#         return Response(
#             {
#                 "status": "success",
#                 "message": (
#                     f"Permohonan surat {surat_type.nama_surat} berhasil diterima dan sedang "
#                     "diproses secara asinkron oleh agen AI."
#                 ),
#                 "request_id": surat_req.id,
#                 "surat_status": surat_req.status,
#                 "data": {
#                     "request_id": surat_req.id,
#                     "kode_surat": surat_type.kode_surat,
#                     "status": surat_req.status,
#                     "approval_id": approval_id, 
#                 },
#             },
#             status=status.HTTP_202_ACCEPTED,
#         )

class ChatThreadCreateAPIView(APIView):
    """
    Endpoint untuk menginisialisasi atau membuat sesi thread chat baru bagi mahasiswa.
    POST /api/surat/chat/init/
    """
    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [CustomJWTAuthentication]

    def post(self, request):
        title = request.data.get("title", "Permohonan Surat Baru")
        document_type = request.data.get("document_type", None)

        # Buat thread baru yang terikat otomatis ke user yang sedang login via JWT
        thread = ChatThread.objects.create(
            user=request.user,
            title=title,
            document_type=document_type,
            status=ChatThread.ThreadStatus.ACTIVE
        )

        AutoSuratSessionManager.save_session(
            thread_id=str(thread.id),
            state="COLLECTING_SLOTS",  # State awal penampungan slot/intent
            intent=document_type,       # Jika document_type dikirim dari frontend, jadikan intent awal
            slots={},
            missing_slots=[]
        )
        
        return Response({
            "status": "success",
            "message": "Sesi chat (thread) berhasil diinisialisasi.",
            "thread_id": str(thread.id),
            "title": thread.title,
            "document_type": thread.document_type,
            "created_at": thread.created_at
        }, status=status.HTTP_201_CREATED)
class SuratTypeListAPIView(ListAPIView):
    """
    Endpoint untuk melihat daftar jenis surat yang tersedia (untuk dropdown form frontend).
    GET /api/surat/types/
    """
    queryset = SuratType.objects.all()
    serializer_class = SuratTypeSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly] 

class SuratRequestListAPIView(ListAPIView):
    """
    Endpoint untuk melihat daftar riwayat pengajuan surat milik mahasiswa yang sedang login.
    GET /api/surat/requests/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SuratRequestListSerializer

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, 'student_profile'):
            return SuratRequest.objects.filter(student=user.student_profile).order_by('-created_at')
        return SuratRequest.objects.all().order_by('-created_at')


class SuratRequestDetailAPIView(RetrieveAPIView):
    """
    Endpoint untuk melihat detail lengkap suatu pengajuan surat beserta status TTE & Approval.
    GET /api/surat/requests/<int:pk>/
    """
    permission_classes = [permissions.IsAuthenticated]
    queryset = SuratRequest.objects.all()
    serializer_class = SuratRequestDetailSerializer

class LecturerApprovalActionAPIView(APIView):
    """
    Endpoint bagi Dosen/Pejabat untuk menyetujui (APPROVE) atau menolak (REJECT) antrean surat.
    Mendelegasikan seluruh eksekusi bisnis persetujuan, nomor, TTE, dan PDF ke `process_approval_action`.
    
    POST /api/surat/approval/<int:approval_id>/action/
    """
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Tindakan Persetujuan atau Penolakan Surat (Dosen/Pejabat)",
        description=(
            "Digunakan oleh Dosen atau Pejabat berwenang untuk menyetujui (APPROVE) "
            "atau menolak (REJECT) antrean pengajuan surat secara manual."
        ),
        request=LecturerApprovalActionRequestSerializer,
        responses={
            200: LecturerApprovalActionResponseSerializer,
            400: ApprovalActionErrorResponseSerializer,
            403: ApprovalActionErrorResponseSerializer,
            404: ApprovalActionErrorResponseSerializer,
        },
    )
    def post(self, request, approval_id):
        action = request.data.get("action")
        if action not in ["APPROVE", "REJECT"]:
            return Response(
                {"error": "Aksi harus bernilai 'APPROVE' atau 'REJECT'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            lecturer_obj = request.user.lecturer_profile
        except (Lecturer.DoesNotExist, AttributeError):
            return Response(
                {"error": "Anda tidak memiliki hak akses sebagai pejabat pemberi persetujuan."},
                status=status.HTTP_403_FORBIDDEN,
            )

        catatan = ""
        if action == "APPROVE":
            serializer = SuratApprovalActionSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            catatan = serializer.validated_data.get("catatan") or ""
        elif action == "REJECT":
            serializer = SuratRejectActionSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            catatan = serializer.validated_data["alasan_penolakan"]

        # Validasi kepemilikan approval step sebelum dilempar ke service
        try:
            approval_step = SuratApproval.objects.select_related('approver').get(id=approval_id)
        except SuratApproval.DoesNotExist:
            return Response({"error": "Antrean approval tidak ditemukan."}, status=status.HTTP_404_NOT_FOUND)

        if approval_step.approver != lecturer_obj:
            return Response({"error": "Anda tidak memiliki hak akses untuk menyetujui antrean ini."}, status=status.HTTP_403_FORBIDDEN)

        # Panggil fungsi service utama yang sudah rapi
        result_data, http_code = process_approval_action(
            approval_id=approval_id,
            lecturer=lecturer_obj,
            action=action,
            catatan=catatan
        )

        return Response(result_data, status=http_code)


class SuratVerifyAPIView(APIView):
    """
    Endpoint publik untuk memverifikasi keaslian surat via QR Code / Token TTE.
    GET /api/surat/verify/<str:token>/
    """
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="Verifikasi Keaslian Dokumen Surat Publik (QR Code / Token TTE)",
        description=(
            "Endpoint publik tanpa autentikasi untuk memverifikasi keabsahan surat resmi "
            "berdasarkan token tanda tangan elektronik (TTE) yang tertera pada QR Code."
        ),
        responses={
            200: SuratVerifyValidResponseSerializer,
            404: SuratVerifyInvalidResponseSerializer,
        },
    )
    def get(self, request, token):
        try:
            tte_sig = TteSignature.objects.select_related(
                'surat_request',
                'surat_request__student',
                'surat_request__student__user',
                'surat_request__student__prodi',
                'surat_request__student__fakultas',
                'surat_request__kode_surat',
                'signer__user'
            ).get(token=token)
        except TteSignature.DoesNotExist:
            return Response({
                "status": "INVALID",
                "message": "Dokumen Surat TIDAK VALID atau palsu. Token verifikasi tidak ditemukan di sistem."
            }, status=status.HTTP_404_NOT_FOUND)

        surat_req = tte_sig.surat_request
        student = surat_req.student

        return Response({
            "status": "VALID",
            "message": "Dokumen Surat ASLI dan terdaftar secara resmi di sistem kampus.",
            "detail_surat": {
                "nomor_surat_resmi": surat_req.nomor_surat_resmi,
                "jenis_surat": surat_req.kode_surat.nama_surat,
                "kategori": surat_req.kode_surat.kategori,
                "tanggal_terbit": surat_req.created_at,
            },
            "mahasiswa": {
                "nim": student.nim,
                "nama": student.user.nama,
                "prodi": student.prodi.nama_prodi,
                "fakultas": student.fakultas.nama_fakultas,
            },
            "tte_info": {
                "hash_sha256": tte_sig.hash_document,
                "penandatangan": tte_sig.signer.user.nama if tte_sig.signer else "Sistem Otomatis Kampus",
                "tanggal_ttd": tte_sig.created_at
            }
        }, status=status.HTTP_200_OK)

from .services.surat_orchestrator import SuratWorkflowOrchestrator
from surat.services.validators import run_validators
import json
class AutoSuratChatAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # pengamanan tambahan: pastikan user memiliki profil mahasiswa
        raw_data = request.data
        if isinstance(raw_data, str):
            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                data = {}
        elif isinstance(raw_data, dict):
            data = raw_data
        else:
            data = {}

        # 1. Ambil payload dari variabel 'data' yang sudah aman (bukan request.data lagi)
        thread_id = data.get("thread_id")  # Bisa None/kosong untuk New Chat
        user_message = data.get("message", "").strip()

        if not user_message:
            return Response(
                {"status": "error", "message": "Pesan tidak boleh kosong."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # 2. Ambil session data terlebih dahulu untuk mengecek apakah sesi sedang berada di tahap konfirmasi
            session_data = AutoSuratSessionManager.get_session(thread_id) if thread_id else {}
            sub_state = session_data.get("sub_state") or session_data.get("state")

            # 3. Jika sesi sedang dalam tahap konfirmasi akhir ("Ya"/"Tidak"), jalankan validasi akademik murni
            if session_data.get("is_waiting_for_confirmation") or sub_state == "WAITING_SLOT_CONFIRMATION":
                positive_keywords = ["ya", "oke", "ok", "benar", "lanjut", "siap", "siyap"]
                is_approved = any(kw in user_message.lower() for kw in positive_keywords)

                if is_approved:
                    kode_surat = session_data.get("intent")
                    slots = session_data.get("slots", {})

                    # Ekstraksi Data Internal Mahasiswa dari Database Profil
                    try:
                        student_obj = request.user.student_profile
                    except AttributeError:
                        return Response({
                            "status": "error",
                            "message": "❌ Akun ini tidak memiliki profil mahasiswa yang terikat."
                        }, status=status.HTTP_400_BAD_REQUEST)

                    student_data = {
                        "nim": student_obj.nim,
                        "nama": getattr(request.user, "nama", ""),
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

                    # Jalankan Uji Validasi Akademik (Murni menggunakan validators.py buatanmu)
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
                        return Response({
                            "status": "error",
                            "message": f"❌ **Pengajuan Ditolak oleh Sistem Akademik Kampus:**\n{error_msg}"
                        }, status=status.HTTP_400_BAD_REQUEST)

            # 4. Lempar ke Orkestrator Utama jika validasi lolos atau alur chat masih berjalan normal
            result = SuratWorkflowOrchestrator.process_incoming_chat(
                thread_id=thread_id,
                user_message=user_message,
                user_obj=request.user
            )
            
            # 4. AMBIL THREAD_ID & CATAT KE DATABASE (ChatMessage)
            resolved_thread_id = result.get("thread_id")
            ai_message_text = result.get("message", "")

            if resolved_thread_id:
                thread_obj = ChatThread.objects.filter(id=resolved_thread_id).first()
                if thread_obj:
                    # Simpan pesan dari Mahasiswa
                    ChatMessage.objects.create(
                        thread=thread_obj,
                        sender_type=ChatMessage.SenderType.USER,
                        message=user_message
                    )
                    # Simpan balasan dari Asisten AI
                    if ai_message_text:
                        ChatMessage.objects.create(
                            thread=thread_obj,
                            sender_type=ChatMessage.SenderType.AI,  # Sesuaikan dengan enum modelmu (AI / ASSISTANT)
                            message=ai_message_text
                        )

            # 5. Kembalikan hasil proses ke frontend
            return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "message": f"Terjadi kesalahan pada sistem: {str(e)}"
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )