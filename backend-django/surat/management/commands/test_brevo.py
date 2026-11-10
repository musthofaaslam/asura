from django.core.management.base import BaseCommand
from surat.services.email_service import send_surat_via_gmail_admin # Sesuaikan dengan lokasi file-mu

class Command(BaseCommand):
    help = 'Test SMTP email via email_service'

    def handle(self, *args, **kwargs):
        self.stdout.write("Mengirim email uji coba via SMTP Brevo...")
        
        result = send_surat_via_gmail_admin(
            recipient_email='cygnusnominus@gmail.com',
            subject='Uji Coba SMTP AutoSurat',
            body_message='Halo Ahmad, ini adalah pesan uji coba pengiriman email menggunakan konfigurasi SMTP Brevo di Django AutoSurat.'
        )
        
        self.stdout.write(str(result))