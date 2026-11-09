from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.postgres.fields import ArrayField
from accounts.models import User, Faculty, Department, Lecturer, Student, Course, StudentCourse

# SARAN PERBAIKAN ARSITEKTURAL:
# 1. Penggunaan `models.TextChoices` (`SuratStatus` dan `ApprovalStatus`) mengunci transisi
#    state machine pada level aplikasi sekaligus mempertahankan nilai string lama (`SUBMITTED`,
#    `IN_REVIEW`, `APPROVED`, `REJECTED`) agar kompatibel penuh dengan `workflow_service.py`,
#    `auto_handlers.py`, dan `manual_handlers.py`.
# 2. Seluruh kolom Agentic AI pada `SuratRequest` (`user_prompt_context`, `ai_original_draft`,
#    `final_approved_content`, `self_critique_score`, `revision_notes`) dikonfigurasi opsional
#    (`null=True, blank=True` atau `default=list`) agar alur pengajuan manual/otomatis eksisting
#    di `views.py` dan `serializers.py` tetap berjalan tanpa breaking changes.
# 3. Model `AgentFeedbackMemory` dilengkapi indeks komposit pada `(kode_surat, is_active)`
#    agar pengambilan aturan gaya bahasa (style rules) untuk injeksi System Prompt Fase 3
#    tetap cepat tanpa membebani query PostgreSQL.

class SuratStatus(models.TextChoices):
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval (Menunggu Tinjauan Draf AI)"
    SUBMITTED = "SUBMITTED", "Submitted (Diajukan)"
    IN_REVIEW = "IN_REVIEW", "In Review (Dalam Proses Persetujuan Berjenjang)"
    APPROVED = "APPROVED", "Approved (Disetujui)"
    REJECTED = "REJECTED", "Rejected (Ditolak)"
    SENT = "SENT", "Sent (Terkirim via Email)"
    PRINTED = "PRINTED", "Printed (Dicetak)"
    FAILED = "FAILED", "Failed (Gagal Eksekusi Pipeline)"
    REVISION_REQUIRED = "REVISION_REQUIRED", "Revision Required (Revisi Draf AI Diminta)"

class ApprovalStatus(models.TextChoices):
    WAITING = "WAITING", "Waiting (Menunggu Giliran)"
    PENDING = "PENDING", "Pending (Aktif Menunggu Keputusan)"
    APPROVED = "APPROVED", "Approved (Disetujui)"
    REJECTED = "REJECTED", "Rejected (Ditolak)"


class SuratType(models.Model):
    kode_surat = models.CharField(max_length=50, primary_key=True)
    nama_surat = models.CharField(max_length=255)
    kategori = models.CharField(max_length=100)
    is_auto_approval = models.BooleanField(default=False)
    template_path = models.CharField(max_length=500, null=True, blank=True)
    required_payload_fields = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = 'surat_types'


class ApprovalRule(models.Model):
    kode_surat = models.ForeignKey(SuratType, on_delete=models.CASCADE)
    step_order = models.IntegerField()
    approval_role = models.CharField(max_length=100)

    class Meta:
        db_table = 'approval_rules'


class SuratNumberCounter(models.Model):
    kode_surat = models.ForeignKey(SuratType, on_delete=models.CASCADE)
    fakultas = models.ForeignKey(Faculty, on_delete=models.CASCADE, null=True, blank=True)
    prodi = models.ForeignKey(Department, on_delete=models.CASCADE, null=True, blank=True)
    tahun = models.IntegerField()
    bulan = models.IntegerField()
    last_number = models.IntegerField(default=0)

    class Meta:
        db_table = 'surat_number_counter'


class SuratRequest(models.Model):
    Status = SuratStatus
    StatusChoices = SuratStatus

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='requests')
    kode_surat = models.ForeignKey(SuratType, on_delete=models.CASCADE)
    nomor_surat_resmi = models.CharField(max_length=100, null=True, blank=True)
    status = models.CharField(
        max_length=50,
        choices=SuratStatus.choices,
        default=SuratStatus.PENDING_APPROVAL,
        db_index=True,
    )
    payload_data = models.JSONField(default=dict)  # Django native JSONField

    # Kolom Agentic AI Fase 1 (Pra-Approval & Self-Reflection) dan Fase 3 (Diffing)
    user_prompt_context = models.TextField(null=True, blank=True)
    ai_original_draft = models.TextField(null=True, blank=True)
    final_approved_content = models.TextField(null=True, blank=True)
    self_critique_score = models.SmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
    )
    revision_notes = models.JSONField(default=list, blank=True)

    file_pdf_url = models.CharField(max_length=500, null=True, blank=True)
    qr_verification_code = models.CharField(max_length=255, null=True, blank=True)
    
    CHOICES_DESTINATION = (
    ('email', 'Email'),
    ('telegram', 'Telegram'),
)
    chosen_destination = models.CharField(choices=CHOICES_DESTINATION, max_length=20, null=True, blank=True)
    target_destination_address = models.CharField(max_length=255, null=True, blank=True) # Bisa menampung email atau chat_id telegram
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'surat_request'


class SuratApproval(models.Model):
    Status = ApprovalStatus
    StatusChoices = ApprovalStatus

    surat_request = models.ForeignKey(SuratRequest, on_delete=models.CASCADE, related_name='approvals')
    step_order = models.IntegerField()
    approver = models.ForeignKey(Lecturer, on_delete=models.CASCADE)
    status = models.CharField(
        max_length=50,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
    )
    catatan = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'surat_approvals'


class SuratAttachment(models.Model):
    surat_request = models.ForeignKey(SuratRequest, on_delete=models.CASCADE, related_name='attachments')
    nama_dokumen = models.CharField(max_length=255)
    file_url = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'surat_attachment'


class TteSignature(models.Model):
    surat_request = models.ForeignKey(SuratRequest, on_delete=models.CASCADE, related_name='signatures')
    signer = models.ForeignKey(Lecturer, on_delete=models.CASCADE, null=True, blank=True)
    hash_document = models.CharField(max_length=255)
    token = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tte_signatures'


class SuratAuditLog(models.Model):
    surat_request = models.ForeignKey(SuratRequest, on_delete=models.CASCADE, related_name='audit_logs')
    action_id = models.CharField(max_length=100)
    action = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'surat_audit_logs'


class AgentFeedbackMemory(models.Model):
    """
    Menyimpan hasil komparasi (diffing) antara `ai_original_draft` dan `final_approved_content`
    serta aturan preferensi gaya bahasa Admin untuk diinjeksikan ke System Prompt pada Fase 3.
    """

    surat_request = models.ForeignKey(
        SuratRequest,
        on_delete=models.CASCADE,
        related_name='feedback_memories',
    )
    kode_surat = models.ForeignKey(
        SuratType,
        on_delete=models.CASCADE,
        related_name='style_memories',
    )
    diff_summary = models.TextField()
    extracted_rule = models.TextField()
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'agent_feedback_memories'
        indexes = [
            models.Index(fields=['kode_surat', 'is_active']),
        ]
        
import uuid
from django.db import models

class ChatThread(models.Model):
    """Menyimpan metadata sesi/thread obrolan mahasiswa."""
    class ThreadStatus(models.TextChoices):
        ACTIVE = 'active', 'Active'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'
        EXPIRED = 'expired', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name='chat_threads'
    )
    title = models.CharField(max_length=255, default="Permohonan Surat Baru")
    document_type = models.CharField(max_length=50, null=True, blank=True) # e.g. surat_keterangan_aktif
    status = models.CharField(
        max_length=20, 
        choices=ThreadStatus.choices, 
        default=ThreadStatus.ACTIVE
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'status']),
        ]

class ChatMessage(models.Model):
    # TAMBAHKAN CLASS ENUM INI DI DALAM MODEL ChatMessage:
    class SenderType(models.TextChoices):
        USER = 'USER', 'User'
        AI = 'AI', 'AI'
        SYSTEM = 'SYSTEM', 'System'

    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name='messages')
    sender_type = models.CharField(
        max_length=20, 
        choices=SenderType.choices, 
        default=SenderType.USER
    )
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[{self.sender_type}] {self.message[:30]}"