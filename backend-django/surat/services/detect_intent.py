import re
from surat.models import SuratType

def detect_intent_dynamically(user_message: str) -> str | None:
    """
    Melakukan pre-filtering / lapis pertama secara deterministik 
    mencocokkan chat user dengan data master surat_types di database.
    """
    message_lower = user_message.lower()
    
    # 1. TAMBAHAN: Kamus Sinonim / Alias untuk singkatan umum
    # Mengantisipasi user yang mengetik bahasa gaul/singkatan lapangan
    explicit_aliases = {
        r'\b(pkl|praktik kerja lapangan|kerja praktek|internship|magang|mbkm magang|surat magang)\b': 'SR-MAG',
        r'\b(beasiswa|sponsorship|bantuan dana|pengajuan beasiswa|surat beasiswa)\b': 'SR-BEA',
    }
    
    # Cek dulu lewat kamus alias cepat
    for pattern, kode in explicit_aliases.items():
        if re.search(pattern, message_lower):
            return kode

    if re.search(r'\bsr-mag\b', message_lower):
        return 'SR-MAG'
    if re.search(r'\bsr-bea\b', message_lower):
        return 'SR-BEA'
    # 2. Ambil semua jenis surat dari database (Supabase)
    # surat_types = SuratType.objects.all()
    
    # for surat in surat_types:
    #     nama_surat_lower = surat.nama_surat.lower()
        
    #     # Ekstraksi kata penting (turunkan batas jadi > 2 agar kata seperti 'pos' atau singkatan lain tertangkap jika ada)
    #     keywords = [kw for kw in re.findall(r'\w+', nama_surat_lower) if len(kw) > 2]
        
    #     # Cek apakah ada keyword dari nama surat yang muncul di chat user
    #     for kw in keywords:
    #         if re.search(r'\b' + re.escape(kw) + r'\b', message_lower):
    #             return surat.kode_surat
                
    #     # Cek langsung berdasarkan kode surat jika user mengetiknya secara spesifik (misal: "SI-MAG")
    #     if surat.kode_surat.lower() in message_lower:
    #         return surat.kode_surat

    # Jika tidak ada keyword yang cocok, kembalikan None (lanjut ke AI Router / Pydantic)
    return None