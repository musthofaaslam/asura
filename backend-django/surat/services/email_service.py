import os
import base64
import logging
import requests
from django.core.mail import EmailMessage
from django.conf import settings

logger = logging.getLogger(__name__)

def send_surat_via_gmail_admin(recipient_email: str, subject: str, body_message: str, pdf_path: str = None):
    """
    Mengirimkan email resmi berisi dokumen surat menggunakan SMTP Brevo.
    Mendukung lampiran file baik dari path lokal maupun URL cloud storage (Cloudinary).
    """
    try:
        # Format body HTML jika diperlukan
        html_body = body_message.replace('\n', '<br>') if body_message else ""
        
        email = EmailMessage(
            subject=subject,
            body=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient_email],
        )
        
        # Django EmailMessage defaultnya plain text, ubah ke HTML content jika ingin support <br>
        email.content_subtype = "html"

        # Jika ada lampiran file PDF (mendukung URL Cloudinary maupun file lokal)
        if pdf_path:
            pdf_bytes = None
            filename = "surat_resmi.pdf"
            
            if pdf_path.startswith("http://") or pdf_path.startswith("https://"):
                # Jika berupa URL publik (seperti Cloudinary)
                resp = requests.get(pdf_path, timeout=15)
                if resp.status_code == 200:
                    pdf_bytes = resp.content
                    filename = pdf_path.split("/")[-1].split("?")[0] or "surat_resmi.pdf"
                else:
                    logger.error(f"Gagal mendownload PDF dari URL: {pdf_path} (Status: {resp.status_code})")
            elif os.path.exists(pdf_path):
                # Jika berupa file lokal server
                with open(pdf_path, "rb") as f:
                    pdf_bytes = f.read()
                filename = os.path.basename(pdf_path)

            if pdf_bytes:
                if not filename.endswith(".pdf"):
                    filename += ".pdf"
                email.attach(filename, pdf_bytes, 'application/pdf')

        # Kirim email via SMTP Django
        email.send(fail_silently=False)
        
        logger.info(f"Email SMTP berhasil terkirim ke {recipient_email}")
        return {
            "status": "SUCCESS", 
            "message": f"Email berhasil dikirim ke {recipient_email}"
        }

    except Exception as e:
        logger.exception(f"Kesalahan saat mengirim email via SMTP: {e}")
        return {"status": "ERROR", "message": str(e)}