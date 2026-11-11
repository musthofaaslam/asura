import os
import base64
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

def send_surat_via_gmail_admin(recipient_email: str, subject: str, body_message: str, pdf_path: str = None):
    """
    Mengirimkan email resmi dokumen surat menggunakan Brevo HTTP API (Port 443).
    Mendukung lampiran file baik dari path lokal maupun URL cloud storage (Cloudinary).
    """
    api_key = getattr(settings, "BREVO_API_KEY", os.environ.get("BREVO_API_KEY", ""))
    
    if not api_key:
        error_msg = "BREVO_API_KEY belum dikonfigurasi di environment / settings.py."
        logger.error(error_msg)
        return {"status": "ERROR", "message": error_msg}

    url = "https://api.brevo.com/v3/smtp/email"
    sender_email = getattr(settings, "DEFAULT_FROM_EMAIL", "AutoSurat Kampus <onboarding@resend.dev>")
    
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json"
    }

    # Format body pesan ke HTML
    html_content = f"<p>{body_message.replace(chr(10), '<br>')}</p>" if body_message else "<p>Berikut terlampir dokumen surat Anda.</p>"

    payload = {
        "sender": {"name": "AutoSurat Kampus", "email": sender_email},
        "to": [{"email": recipient_email}],
        "subject": subject,
        "htmlContent": html_content
    }

    # Jika ada lampiran file PDF (mendukung URL Cloudinary maupun file lokal)
    if pdf_path:
        try:
            pdf_bytes = None
            filename = "surat_resmi.pdf"
            
            if pdf_path.startswith("http://") or pdf_path.startswith("https://"):
                # Opsi menggunakan Cloudinary Admin/Download SDK atau fallback requests dengan signed URL
                # Jika URL dari Cloudinary, kita bisa ambil public_id-nya atau gunakan cloudinary.utils.private_download_url
                try:
                    import cloudinary.utils
                    # Ekstrak public_id atau gunakan URL langsung dengan tanda tangan SDK jika privat
                    # Atau cara paling aman: jika file ada di local temp, gunakan path lokalnya.
                    # Tapi jika terpaksa via URL Cloudinary, kita bisa generate signed download URL:
                    parsed_url = pdf_path.split("/upload/")
                    if len(parsed_url) > 1:
                        # Hilangkan versi (misal v179435...) jika ada
                        sub_path = parsed_url[1]
                        if sub_path.startswith("v"):
                            sub_path = "/".join(sub_path.split("/")[1:])
                        public_id_with_ext = sub_path.split("?")[0]
                        public_id = public_id_with_ext.rsplit(".", 1)[0]
                        
                        # Generate signed download URL via Cloudinary SDK
                        signed_download_url = cloudinary.utils.private_download_url(
                            public_id, 
                            resource_type="raw", # Sesuaikan dengan saat upload
                            format="pdf"
                        )
                        resp = requests.get(signed_download_url, timeout=15)
                    else:
                        resp = requests.get(pdf_path, timeout=15)
                        
                    if resp.status_code == 200:
                        pdf_bytes = resp.content
                        filename = pdf_path.split("/")[-1].split("?")[0] or "surat_resmi.pdf"
                    else:
                        logger.error(f"Gagal mendownload PDF via signed URL Cloudinary: (Status: {resp.status_code})")
                except Exception as sdk_err:
                    logger.error(f"Gagal memproses Cloudinary SDK download: {sdk_err}")
                    # Fallback ke requests biasa
                    resp = requests.get(pdf_path, timeout=15)
                    if resp.status_code == 200:
                        pdf_bytes = resp.content
            elif os.path.exists(pdf_path):
                with open(pdf_path, "rb") as f:
                    pdf_bytes = f.read()
                filename = os.path.basename(pdf_path)

            if pdf_bytes:
                if not filename.endswith(".pdf"):
                    filename += ".pdf"
                
                pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
                payload["attachment"] = [
                    {
                        "content": pdf_base64,
                        "name": filename
                    }
                ]
        except Exception as attach_err:
            logger.error(f"Gagal memproses lampiran PDF untuk Brevo API: {attach_err}")
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        res_data = response.json()

        if response.status_code in [200, 201]:
            message_id = res_data.get("messageId")
            logger.info(f"Email Brevo API berhasil terkirim ke {recipient_email}. MessageID: {message_id}")
            return {
                "status": "SUCCESS", 
                "message": f"Email berhasil dikirim ke {recipient_email}",
                "message_id": message_id
            }
        else:
            logger.error(f"Gagal mengirim email via Brevo API: {res_data}")
            return {"status": "ERROR", "message": str(res_data)}

    except Exception as e:
        logger.exception(f"Kesalahan koneksi saat mengakses Brevo API: {e}")
        return {"status": "ERROR", "message": str(e)}