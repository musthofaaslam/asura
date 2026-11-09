from enum import Enum
from pydantic import BaseModel, Field
from google import genai  # atau library client LLM yang kamu gunakan

# 1. Definisikan Enum kaku berdasarkan daftar kode surat resmi di database
class SuratIntentEnum(str, Enum):
    SR_BEA = "SR-BEA"
    SR_MAG = "SR-MAG"

# 2. Definisikan Struktur Output Pydantic
class IntentRouterOutput(BaseModel):
    kode_surat: SuratIntentEnum | None = Field(
        description="Kode surat resmi yang paling sesuai dengan maksud pengguna. Jika sama sekali tidak jelas, kembalikan null."
    )
    alasan: str = Field(description="Alasan singkat mengapa kode surat tersebut dipilih.")

def route_intent_with_ai(user_message: str) -> str | None:
    """
    Lapis kedua: Menggunakan AI dengan Pydantic Structured Outputs 
    ketika keyword matching gagal (mengembalikan None).
    """
    client = genai.Client() # Inisialisasi client LLM
    
    prompt = f"""
    Analisis pesan dari mahasiswa berikut dan tentukan apakah pesan tersebut secara spesifik meminta pembuatan dokumen:
    1. "SR-MAG" (untuk Surat Rekomendasi Pkl, Praktik Kerja Lapangan, Kerja Praktek, Internship, Magang, MBKM)
    2. "SR-BEA" (untuk Surat Rekomendasi Beasiswa, sponsorship, atau bantuan dana)

    Pesan User: "{user_message}"

    Aturan Ketat:
    - Pilih HANYA salah satu dari enum yang tersedia jika maksudnya jelas mengarah ke magang atau beasiswa.
    - Jika pesan bersifat umum, menanyakan hal lain (misal: transkrip nilai, jadwal, sponsor dana di luar beasiswa formal, atau curhat biasa), atau sama sekali tidak jelas, WAJIB set nilai kode_surat menjadi null. Jangan dipaksa-paksakan!
    """

    try:
        # Memanggil LLM dengan paksaan struktur Pydantic (Structured Outputs)
        response = client.models.generate_content(
            model='gemini-2.5-flash',  # atau model yang kamu gunakan
            contents=prompt,
            config={
                'response_mime_type': 'application/json',
                'response_schema': IntentRouterOutput,
                'temperature': 0.0,  # Set 0 agar deterministik dan tidak berhalusinasi
            },
        )
        
        # Parse hasil JSON dari AI ke model Pydantic
        result = IntentRouterOutput.model_validate_json(response.text)
        return result.kode_surat.value if result.kode_surat else None

    except Exception as e:
        # Jaring pengaman jika AI error (misal: 503 atau format meleset)
        print(f"AI Router Error: {e}")
        return None