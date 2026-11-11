# apps/surat/services/workflow_service.py
from logging import getLogger
import os
from django.utils import timezone
from django.db import transaction
from .auto_handlers import handle_auto_approval_surat
from .manual_handlers import handle_manual_approval_surat
from .number_service import generate_surat_number
from .tte_service import create_tte_signature
from .pdf_service import render_and_upload_pdf
from ..models import SuratApproval, SuratAuditLog, SuratRequest
from .email_service import send_surat_via_gmail_admin
from .telegram_service import send_surat_via_telegram
import threading
from django.db import close_old_connections
import tempfile
from django.template.loader import render_to_string
from weasyprint import HTML

logger = getLogger(__name__)

def process_surat_request(student, surat_type, payload_data, attachments_files=None):
    """
    Router utama untuk mengarahkan pengajuan ke jalur Auto atau Manual.
    """
    if surat_type.is_auto_approval:
        return handle_auto_approval_surat(student, surat_type, payload_data, attachments_files)
    else:
        return handle_manual_approval_surat(student, surat_type, payload_data, attachments_files)


def process_approval_action(approval_id, lecturer, action, catatan=""):
    """
    Router saat Dosen melakukan APPROVE atau REJECT pada antrean surat.
    action: 'APPROVE' atau 'REJECT'
    """
    try:
        approval_step = SuratApproval.objects.select_related(
            'surat_request', 
            'surat_request__kode_surat', 
            'surat_request__student', 
            'surat_request__student__fakultas',
            'surat_request__student__prodi'
        ).get(id=approval_id)
    except SuratApproval.DoesNotExist:
        return {"error": "Antrean approval tidak ditemukan."}, 404

    surat_req = approval_step.surat_request

    # Validasi kepemilikan approver
    if approval_step.approver != lecturer:
        return {"error": "Anda tidak memiliki hak akses untuk menyetujui antrean ini."}, 403

    if approval_step.status != 'PENDING':
        return {"error": "Langkah persetujuan ini belum aktif atau sudah diproses sebelumnya."}, 400

    # Handle REJECT
    if action == 'REJECT':
        approval_step.status = 'REJECTED'
        approval_step.catatan = catatan
        approval_step.save(update_fields=['status', 'catatan'])

        surat_req.status = 'REJECTED'
        surat_req.save(update_fields=['status'])

        SuratAuditLog.objects.create(
            surat_request=surat_req,
            action_id="REJ_01",
            action=f"REJECTED_BY_{lecturer.id}"
        )
        return {"message": "Pengajuan surat telah ditolak."}, 200

    # Handle APPROVE
    if action == 'APPROVE':
        approval_step.status = 'APPROVED'
        approval_step.catatan = catatan
        approval_step.save(update_fields=['status', 'catatan'])

        # Cek apakah masih ada step approval berikutnya
        next_step = SuratApproval.objects.filter(
            surat_request=surat_req, 
            step_order=approval_step.step_order + 1
        ).first()

        if next_step:
            # Aktifkan step berikutnya menjadi PENDING
            next_step.status = 'PENDING'
            next_step.save(update_fields=['status'])
            surat_req.status = 'IN_REVIEW'
            surat_req.save(update_fields=['status'])

            SuratAuditLog.objects.create(
                surat_request=surat_req,
                action_id=f"APP_0{approval_step.step_order}",
                action=f"APPROVED_STEP_{approval_step.step_order}_FORWARDED"
            )
            return {"message": "Persetujuan tahap ini berhasil. Surat diteruskan ke pejabat selanjutnya."}, 200
        else:
            # Jika ini step terakhir, terbitkan surat resmi secara penuh!
            surat_req.status = 'APPROVED'
            
            try:
                # Bungkus proses akhir dengan transaksi agar aman
                with transaction.atomic():
                    # 1. Generate Nomor Surat Resmi
                    nomor_resmi = generate_surat_number(surat_req.kode_surat, fakultas=surat_req.student.fakultas)
                    surat_req.nomor_surat_resmi = nomor_resmi
                    surat_req.kode_surat.template_path = "kerangka-surat/base_surat.html"

                    # 2. Generate TTE & QR Code
                    tte_data = create_tte_signature(surat_req, signer=lecturer)
                    surat_req.qr_verification_code = tte_data['qr_code_url']

                    # Tentukan teks konten final yang akan dicetak
                    resolved_content = (
                        surat_req.final_approved_content 
                        or surat_req.ai_original_draft 
                        or ""
                    )

                    # Ambil daftar antrean approval yang sudah disetujui/terkait untuk dilempar ke template
                    surat_approvals = SuratApproval.objects.filter(surat_request=surat_req).order_by('step_order')
                    surat_approvals_list = []
                    
                    # Generate Base64 untuk QR Code TTE agar WeasyPrint pasti bisa merendernya
                    import qrcode, io, base64
                    qr_img = qrcode.make(tte_data.get('token', nomor_resmi))
                    buf = io.BytesIO()
                    qr_img.save(buf, format="PNG")
                    qr_base64 = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"

                    surat_approvals_list = []
                    total_steps = len(surat_approvals)

                    for index, app in enumerate(surat_approvals):
                        step_order = getattr(app, 'step_order', None)
                        status = getattr(app, 'status', None)
                        
                        lecturer_obj = getattr(app, 'approver', None)
                        user_obj = getattr(lecturer_obj, 'user', None) if lecturer_obj else None
                        
                        approver_name = getattr(user_obj, 'nama', None) or getattr(lecturer_obj, 'nama', None) or str(lecturer_obj)
                        approver_nip = getattr(user_obj, 'nip_nim', None) or getattr(lecturer_obj, 'nip_nim', '-')
                        
                        if step_order == 1:
                            approver_role = "Dosen Pembimbing"
                        else:
                            approver_role = "Pimpinan Fakultas"

                        is_last = (index == total_steps - 1)
                        qr_url = qr_base64 if is_last else None

                        surat_approvals_list.append({
                            "step_order": step_order,
                            "approver_role": approver_role,
                            "approver_name": approver_name,
                            "approver_nip": approver_nip,
                            "status": status,
                            "qr_code_url": qr_url
                        })

                    context_data = {
                        "student": surat_req.student,
                        "payload": surat_req.payload_data,
                        "nomor_surat": nomor_resmi,
                        "nama_surat": surat_req.kode_surat.nama_surat,
                        "qr_code_url": qr_base64,
                        "final_content": resolved_content,
                        "approvals": surat_approvals_list,
                        "tanggal_terbit": timezone.now(),
                    }
                    
                    # 💡 MODIFIKASI DI SINI: Kita render file lokal secara manual sebentar 
                    # agar path lokalnya bisa langsung dipakai untuk lampiran email, 
                    # lalu tetap di-upload ke Cloudinary untuk arsip database.
                    
                    html_string = render_to_string(surat_req.kode_surat.template_path, context_data)
                    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as output_pdf:
                        HTML(string=html_string).write_pdf(output_pdf.name)
                    
                        local_pdf_path = output_pdf.name  # <-- Ini path file lokal yang 100% aman!

                    # Upload ke Cloudinary untuk URL database
                    pdf_url = render_and_upload_pdf(surat_req.kode_surat.template_path, context_data)
                    
                    surat_req.file_pdf_url = pdf_url
                    surat_req.status = 'SENT'  
                    surat_req.save()

                # 4. Catat Audit Log Final
                SuratAuditLog.objects.create(
                    surat_request=surat_req,
                    action_id="FIN_01",
                    action="FINAL_APPROVED_AND_ISSUED"
                )

                # dist_thread = threading.Thread(
                #     target=_execute_distribution_worker,
                #     args=(surat_req.id, local_pdf_path),
                #     name=f"async-dist-{surat_req.id}",
                #     daemon=True
                # )
                # dist_thread.start()
                _execute_distribution_worker(surat_req.id, local_pdf_path)

            except Exception as e:
                return {"error": f"Gagal menerbitkan dokumen resmi: {str(e)}"}, 500

            return {
                "message": "Persetujuan final selesai. Surat resmi berhasil diterbitkan dan dikirim ke tujuan!",
                "nomor_surat_resmi": nomor_resmi,
                "file_pdf_url": surat_req.file_pdf_url
            }, 200

    return {"error": "Aksi tidak valid."}, 400

def route_and_distribute_document(surat_request, local_pdf_path: str = None):
    """
    Router utama agen untuk menentukan apakah kirim ke Email atau Telegram
    menggunakan kolom database yang konsisten (`chosen_destination` dan `target_destination_address`).
    """
    # Pastikan file PDF target valid (prioritaskan path lokal yang benar-benar ada)
    pdf_target = None
    if local_pdf_path and os.path.exists(local_pdf_path):
        pdf_target = local_pdf_path
    else:
        pdf_target = surat_request.file_pdf_url

    if not pdf_target:
        raise ValueError("File PDF belum tersedia untuk didistribusikan.")

    destination_type = surat_request.chosen_destination  # 'email' atau 'telegram'
    target_address = surat_request.target_destination_address # Berisi email atau chat ID

    if destination_type == 'email':
        subject = f"Dokumen Resmi - {surat_request.kode_surat.nama_surat}"
        body = (
            "Dengan hormat,\n\n"
            "Berikut dilampirkan dokumen resmi yang telah diterbitkan dan disetujui oleh Fakultas.\n"
            "Anda juga dapat melakukan verifikasi keaslian dokumen melalui QR Code pada file terlampir.\n\n"
            "Terima kasih."
        )
        return send_surat_via_gmail_admin(
            recipient_email=target_address,
            subject=subject,
            body_message=body,
            pdf_path=pdf_target  
        )
        
    elif destination_type == 'telegram':
        caption = "✅ *Surat Resmi Anda Telah Disetujui!*\nBerikut adalah dokumen PDF resmi yang diterbitkan oleh sistem kampus."
        return send_surat_via_telegram(
            chat_id=target_address,
            caption=caption,
            pdf_path=pdf_target
        )
    else:
        logger.warning(f"Destinasi pengiriman tidak dikenal atau kosong untuk SuratRequest id={surat_request.id}")        
def _execute_distribution_worker(surat_request_id: int, local_pdf_path: str = None):
    """
    Worker asinkron untuk menjalankan route_and_distribute_document di latar belakang
    agar tidak memblokir HTTP response aksi approval dosen.
    """
    close_old_connections()
    try:
        surat_req = SuratRequest.objects.get(pk=surat_request_id)
        logger.info(f"[ASYNC_DIST_START] Memulai distribusi asinkron untuk SuratRequest id={surat_req.id}")
        
        # 🔑 Tangkap hasil pengiriman dari router
        distribution_result = route_and_distribute_document(surat_req, local_pdf_path=local_pdf_path)
        
        # 🔑 Cek apakah hasilnya benar-benar sukses atau error dari service
        if isinstance(distribution_result, dict) and distribution_result.get("status") == "ERROR":
            raise Exception(f"Gagal dari API pengiriman: {distribution_result.get('message')}")
        
        surat_req.status = "SENT"
        surat_req.save(update_fields=["status"])
        
        SuratAuditLog.objects.create(
            surat_request=surat_req,
            action_id="DIST_SUCCESS",
            action=f"ASYNC_SENT_VIA_{str(surat_req.chosen_destination).upper()}"
        )
        logger.info(f"[ASYNC_DIST_SUCCESS] Dokumen berhasil dikirim untuk SuratRequest id={surat_req.id}")
        
    except Exception as exc:
        logger.exception(f"[ASYNC_DIST_ERROR] Gagal mengirim dokumen di latar belakang: {exc}")
        try:
            surat_req = SuratRequest.objects.get(pk=surat_request_id)
            surat_req.status = "DISTRIBUTION_FAILED"
            surat_req.save(update_fields=["status"])
            SuratAuditLog.objects.create(
                surat_request=surat_req,
                action_id="DIST_FAILED",
                action=f"ASYNC_FAILED: {str(exc)}"[:255]
            )
        except Exception:
            pass
    finally:
        close_old_connections()