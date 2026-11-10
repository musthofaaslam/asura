from django.core.management.base import BaseCommand
from django.utils import timezone
from surat.services.email_service import send_surat_via_gmail_admin
class Command(BaseCommand):
    help = "Menguji coba pengiriman email via Brevo HTTP API"

    def handle(self, *args, **options):
        self.stdout.write("Sedang mengirim email uji coba ke Brevo...")
        
        # Masukkan email tujuan uji coba (bisa email pribadimu yang lain)
        recipient = "musthofaaslam29@gmail.com"
        subject = f"Uji Coba Unik AutoSurat - {timezone.now().strftime('%H:%M:%S')}"
        # subject = "Uji Coba Sistem AutoSurat Brevo"
        body = "Halo, ini adalah pesan uji coba dari backend Django AutoSurat menggunakan Brevo HTTP API."
        
        result = send_surat_via_gmail_admin(
            recipient_email=recipient,
            subject=subject,
            body_message=body
        )
        
        if result.get("status") == "SUCCESS":
            self.stdout.write(self.style.SUCCESS(f"SUKSES! {result.get('message')} (ID: {result.get('email_id')})"))
        else:
            self.stdout.write(self.style.ERROR(f"GAGAL: {result.get('message')}"))