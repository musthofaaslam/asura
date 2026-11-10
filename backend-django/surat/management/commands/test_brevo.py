from django.core.management.base import BaseCommand
from surat.services.email_service import send_surat_via_gmail_admin

class Command(BaseCommand):
    help = "Menguji coba pengiriman email via Brevo HTTP API (Port 443)"

    def add_arguments(self, parser):
        # Opsional: memungkinkan kamu memasukkan email penerima kustom via terminal
        parser.add_argument(
            '--email', 
            type=str, 
            default='musthofaaslam29@gmail.com', 
            help='Alamat email penerima uji coba'
        )

    def handle(self, *args, **options):
        recipient = options['email']
        subject = "Uji Coba Sistem AutoSurat Brevo HTTP API"
        body = "Halo Ahmad,\n\nIni adalah pesan uji coba dari backend Django AutoSurat menggunakan jalur Brevo HTTP API (Port 443)."

        self.stdout.write(f"Sedang mengirim email uji coba ke {recipient}...")

        result = send_surat_via_gmail_admin(
            recipient_email=recipient,
            subject=subject,
            body_message=body
        )

        if result.get("status") == "SUCCESS":
            msg_id = result.get('message_id', 'N/A')
            self.stdout.write(
                self.style.SUCCESS(f"✅ SUKSES! {result.get('message')} (MessageID: {msg_id})")
            )
        else:
            self.stdout.write(
                self.style.ERROR(f"❌ GAGAL: {result.get('message')}")
            )