import json
from rest_framework import serializers
from .models import (
    SuratType,
    SuratRequest,
    SuratApproval,
    SuratAttachment,
    TteSignature,
    SuratAuditLog
)
from .services.validators import run_validators

# SARAN PERBAIKAN ARSITEKTURAL:
# 1. Pisahkan kontrak serializer untuk skema dokumentasi OpenAPI (`drf-spectacular`)
#    secara tegas agar setiap endpoint `APIView` memiliki definisi request dan response
#    yang tervalidasi tanpa memicu error `unable to guess serializer`.
# 2. `SuratRequestCreateSerializer` mendukung alur Agentic AI Fase 1 melalui atribut
#    `user_prompt_context` (serta alias `konteks`), sehingga validasi sinkron tidak memblokir
#    permohonan berbasis bahasa alami sebelum `SuratDraftingAgent` mengekstrak `payload_data`
#    di latar belakang.
# 3. `SuratApprovalActionSerializer` dan `LecturerApprovalActionRequestSerializer`
#    menerima koreksi `final_approved_content` serta pilihan `final_status` (`SENT` / `PRINTED`)
#    dari Dosen/Pejabat saat menyetujui draf yang berstatus `PENDING_APPROVAL`.


class SuratRequestCreateSerializer(serializers.Serializer):
    """
    Serializer khusus untuk VALIDASI input saat mahasiswa mengajukan surat baru.
    Menerima kode_surat, konteks bahasa alami (user_prompt_context / konteks),
    payload JSON dinamis opsional, dan file lampiran (attachments).
    """
    kode_surat = serializers.CharField(max_length=50)
    user_prompt_context = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Konteks permohonan surat dalam bahasa alami untuk diproses oleh SuratDraftingAgent.",
    )
    konteks = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        write_only=True,
        help_text="Alias opsional untuk konteks permohonan surat.",
    )
    payload_data = serializers.JSONField(default=dict, required=False)
    
    chosen_destination = serializers.ChoiceField(
        choices=["email", "telegram"],
        required=False,
        allow_null=True,
        help_text="Pilihan kanal pengiriman dokumen akhir ('email' atau 'telegram')."
    )
    target_destination_address = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Alamat email atau Chat ID Telegram tujuan."
    )
    
    attachments = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        allow_empty=True
    )

    def validate(self, data):
        kode_surat = data.get('kode_surat')
        raw_payload = data.get('payload_data')

        prompt_candidates = [
            data.get('user_prompt_context'),
            data.get('konteks'),
        ]
        if hasattr(self, 'initial_data') and isinstance(self.initial_data, dict):
            prompt_candidates.extend([
                self.initial_data.get('prompt'),
                self.initial_data.get('context'),
            ])

        if isinstance(raw_payload, str) and raw_payload.strip():
            prompt_candidates.append(raw_payload.strip())
            payload = {}
        elif isinstance(raw_payload, dict):
            payload = dict(raw_payload)
            for key_alias in ('user_prompt_context', 'konteks', 'prompt', 'context'):
                alias_val = payload.get(key_alias)
                if isinstance(alias_val, str) and alias_val.strip():
                    prompt_candidates.append(alias_val.strip())
        elif raw_payload is None:
            payload = {}
        else:
            raise serializers.ValidationError({
                "payload_data": "Format payload_data harus berupa objek JSON (dictionary)."
            })

        resolved_prompt = next(
            (item.strip() for item in prompt_candidates if isinstance(item, str) and item.strip()),
            ""
        )

        # Apabila user tidak mengirim prompt eksplisit tetapi mengirim atribut di payload_data,
        # sintesiskan konteks teks dari payload_data agar SuratDraftingAgent tetap dapat bekerja.
        if not resolved_prompt and payload:
            resolved_prompt = json.dumps(payload, ensure_ascii=True)

        if not resolved_prompt:
            raise serializers.ValidationError({
                "user_prompt_context": (
                    "Konteks permohonan surat (user_prompt_context / konteks) atau "
                    "payload_data wajib diisi."
                )
            })

        data['user_prompt_context'] = resolved_prompt
        data['payload_data'] = payload

        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            raise serializers.ValidationError("Autentikasi diperlukan untuk mengajukan surat.")

        user = request.user
        if hasattr(user, 'student_profile'):
            student_profile = user.student_profile
            student_data = {
                "nim": student_profile.nim,
                "status_akademik": student_profile.status_akademik,
                "status_pembayaran_ukt": student_profile.status_pembayaran_ukt,
                "semester_aktif": student_profile.semester_aktif,
                "total_sks_lulus": student_profile.total_sks_lulus,
                "ipk_kumulatif": student_profile.ipk_kumulatif,
                "catatan_pelanggaran_etik": student_profile.catatan_pelanggaran_etik,
                "status_disiplin_kampus": student_profile.status_disiplin_kampus,
                "status_beasiswa_aktif": student_profile.status_beasiswa_aktif,
                "nama_beasiswa_aktif": student_profile.nama_beasiswa_aktif,
                "prodi": student_profile.prodi.nama_prodi,
                "fakultas": student_profile.fakultas.nama_fakultas,
            }

            student_courses_data = list(
                student_profile.courses_taken.values(
                    "student_id", "course_id", "semester_taken", "status_krs"
                )
            )

            run_validators(
                kode_surat=kode_surat,
                student_data=student_data,
                payload=payload,
                student_courses_data=student_courses_data,
            )

        return data

class ReDraftSuratRequestSerializer(serializers.Serializer):
    additional_prompt_context = serializers.CharField(
        required=True,
        allow_blank=False,
        help_text="Konteks tambahan atau jawaban atas revision_notes dari agen AI."
    )
class SuratApprovalActionSerializer(serializers.Serializer):
    """Serializer untuk input catatan opsional dan teks final saat Dosen melakukan APPROVE"""
    catatan = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    final_approved_content = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Naskah surat final yang telah ditinjau atau disunting oleh Dosen/Pejabat.",
    )
    final_status = serializers.ChoiceField(
        choices=["SENT", "PRINTED"],
        required=False,
        default="SENT",
        help_text="Status akhir surat setelah eksekusi TTE dan PDF selesai ('SENT' atau 'PRINTED').",
    )


class SuratRejectActionSerializer(serializers.Serializer):
    """Serializer untuk input alasan wajib saat Dosen melakukan REJECT"""
    alasan_penolakan = serializers.CharField(required=True, allow_blank=False)


class SuratTypeSerializer(serializers.ModelSerializer):
    """Serializer output untuk master data jenis surat (misal untuk pilihan dropdown frontend)"""
    class Meta:
        model = SuratType
        fields = ['kode_surat', 'nama_surat', 'kategori', 'is_auto_approval', 'template_path']


class SuratAttachmentSerializer(serializers.ModelSerializer):
    """Serializer output untuk file lampiran mahasiswa"""
    class Meta:
        model = SuratAttachment
        fields = ['id', 'nama_dokumen', 'file_url', 'mime_type', 'created_at']


class TteSignatureSerializer(serializers.ModelSerializer):
    """Serializer output untuk informasi TTE & QR Code verifikasi"""
    signer_name = serializers.CharField(source='signer.user.nama', read_only=True, default="Sistem Otomatis")
    signer_nip = serializers.CharField(source='signer.user.nip_nim', read_only=True, default=None)

    class Meta:
        model = TteSignature
        fields = ['id', 'signer_name', 'signer_nip', 'hash_document', 'token', 'created_at']


class SuratApprovalSerializer(serializers.ModelSerializer):
    """Serializer output untuk timeline antrean persetujuan dosen/pejabat"""
    approver_name = serializers.CharField(source='approver.user.nama', read_only=True, default=None)
    approver_nip = serializers.CharField(source='approver.user.nip_nim', read_only=True, default=None)

    class Meta:
        model = SuratApproval
        fields = [
            'id',
            'step_order',
            'approver',
            'approver_name',
            'approver_nip',
            'status',
            'catatan',
            'created_at'
        ]


class SuratAuditLogSerializer(serializers.ModelSerializer):
    """Serializer output untuk rekam jejak aktivitas (audit log) surat"""
    class Meta:
        model = SuratAuditLog
        fields = ['id', 'action_id', 'action', 'created_at']


class SuratRequestListSerializer(serializers.ModelSerializer):
    """Serializer output ringkas untuk menampilkan daftar riwayat surat di dashboard"""
    nama_surat = serializers.CharField(source='kode_surat.nama_surat', read_only=True)
    kategori = serializers.CharField(source='kode_surat.kategori', read_only=True)
    nim_mahasiswa = serializers.CharField(source='student.nim', read_only=True)
    nama_mahasiswa = serializers.CharField(source='student.user.nama', read_only=True)

    class Meta:
        model = SuratRequest
        fields = [
            'id',
            'nim_mahasiswa',
            'nama_mahasiswa',
            'kode_surat',
            'nama_surat',
            'kategori',
            'nomor_surat_resmi',
            'status',
            'created_at'
        ]


class SuratRequestDetailSerializer(serializers.ModelSerializer):
    """
    Serializer output detail lengkap berikut seluruh relasi bersarang (nested)
    seperti lampiran, timeline approval, TTE, dan audit logs.
    """
    nama_surat = serializers.CharField(source='kode_surat.nama_surat', read_only=True)
    nama_mahasiswa = serializers.CharField(source='student.user.nama', read_only=True)
    nim_mahasiswa = serializers.CharField(source='student.nim', read_only=True)
    prodi_mahasiswa = serializers.CharField(source='student.prodi.nama_prodi', read_only=True)
    fakultas_mahasiswa = serializers.CharField(source='student.fakultas.nama_fakultas', read_only=True)

    attachments = SuratAttachmentSerializer(many=True, read_only=True)
    approvals = SuratApprovalSerializer(many=True, read_only=True)
    signatures = TteSignatureSerializer(many=True, read_only=True)
    audit_logs = SuratAuditLogSerializer(many=True, read_only=True)

    class Meta:
        model = SuratRequest
        fields = [
            'id',
            'nim_mahasiswa',
            'nama_mahasiswa',
            'prodi_mahasiswa',
            'fakultas_mahasiswa',
            'kode_surat',
            'nama_surat',
            'nomor_surat_resmi',
            'status',
            'payload_data',
            'user_prompt_context',
            'ai_original_draft',
            'final_approved_content',
            'self_critique_score',
            'revision_notes',
            'file_pdf_url',
            'qr_verification_code',
            'created_at',
            'attachments',
            'approvals',
            'signatures',
            'audit_logs'
        ]


class SuratRequestSuccessResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default="success")
    message = serializers.CharField(default="Pengajuan surat berhasil dibuat.")
    data = serializers.DictField(default={"request_id": 123, "nomor_surat": "B/123/UNIV/2026"})


class LecturerApprovalActionRequestSerializer(serializers.Serializer):
    """
    Serializer skema request terpadu untuk aksi APPROVE atau REJECT oleh Dosen/Pejabat.
    """
    action = serializers.ChoiceField(
        choices=["APPROVE", "REJECT"],
        required=True,
        help_text="Tindakan persetujuan: 'APPROVE' atau 'REJECT'.",
    )
    catatan = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Catatan opsional saat melakukan APPROVE.",
    )
    final_approved_content = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Naskah surat final hasil tinjauan atau koreksi Dosen/Pejabat saat APPROVE.",
    )
    final_status = serializers.ChoiceField(
        choices=["SENT", "PRINTED"],
        required=False,
        default="SENT",
        help_text="Status distribusi akhir surat setelah PDF dan TTE diterbitkan ('SENT' atau 'PRINTED').",
    )
    alasan_penolakan = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Alasan penolakan (wajib diisi apabila action bernilai 'REJECT').",
    )


class LecturerApprovalActionResponseSerializer(serializers.Serializer):
    """
    Serializer skema response sukses saat Dosen melakukan APPROVE atau REJECT.
    """
    message = serializers.CharField(
        default="Persetujuan final selesai. Surat resmi berhasil diterbitkan!"
    )
    nomor_surat_resmi = serializers.CharField(
        required=False,
        allow_null=True,
        default="B/123/UNIV/2026",
    )
    file_pdf_url = serializers.URLField(
        required=False,
        allow_null=True,
        default="https://res.cloudinary.com/demo/image/upload/surat.pdf",
    )


class ApprovalActionErrorResponseSerializer(serializers.Serializer):
    """
    Serializer skema response error pada aksi persetujuan surat.
    """
    error = serializers.CharField(
        default="Anda tidak memiliki hak akses untuk menyetujui antrean ini."
    )


class VerifiedSuratDetailSerializer(serializers.Serializer):
    nomor_surat_resmi = serializers.CharField(allow_null=True)
    jenis_surat = serializers.CharField()
    kategori = serializers.CharField()
    tanggal_terbit = serializers.DateTimeField()


class VerifiedMahasiswaSerializer(serializers.Serializer):
    nim = serializers.CharField()
    nama = serializers.CharField()
    prodi = serializers.CharField()
    fakultas = serializers.CharField()


class VerifiedTteInfoSerializer(serializers.Serializer):
    hash_sha256 = serializers.CharField()
    penandatangan = serializers.CharField()
    tanggal_ttd = serializers.DateTimeField()


class SuratVerifyValidResponseSerializer(serializers.Serializer):
    """
    Serializer skema response publik saat token QR Code / TTE dinyatakan VALID.
    """
    status = serializers.CharField(default="VALID")
    message = serializers.CharField(
        default="Dokumen Surat ASLI dan terdaftar secara resmi di sistem kampus."
    )
    detail_surat = VerifiedSuratDetailSerializer()
    mahasiswa = VerifiedMahasiswaSerializer()
    tte_info = VerifiedTteInfoSerializer()


class SuratVerifyInvalidResponseSerializer(serializers.Serializer):
    """
    Serializer skema response publik saat token QR Code / TTE tidak ditemukan (INVALID).
    """
    status = serializers.CharField(default="INVALID")
    message = serializers.CharField(
        default="Dokumen Surat TIDAK VALID atau palsu. Token verifikasi tidak ditemukan di sistem."
    )



class ChatMessageInputSerializer(serializers.Serializer):
    """Serializer input untuk Single Endpoint Chat Mahasiswa"""
    thread_id = serializers.UUIDField(
        required=True,
        help_text="UUID dari thread chat aktif."
    )
    message = serializers.CharField(
        required=True,
        allow_blank=False,
        help_text="Pesan atau jawaban yang diketik oleh mahasiswa."
    )
    
class ChatMessageResponseSerializer(serializers.Serializer):
    """Serializer output untuk respons dari Orkestrator Chat"""
    status = serializers.CharField(default="success")
    state = serializers.CharField(help_text="State aktif sesi saat ini (misal: COLLECTING_SLOTS, READY_TO_DRAFT)")
    reply = serializers.CharField(help_text="Balasan teks dari asisten AI/Bot")
    detected_intent = serializers.CharField(required=False, allow_null=True)
    collected_slots = serializers.JSONField(required=False, default=dict)
    required_fields = serializers.ListField(required=False, default=list)