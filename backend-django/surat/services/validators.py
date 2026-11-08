from rest_framework import serializers
from .helpers import (
    validate_payment_status,
    validate_active_status,
    check_semester_registration,
    verify_ethical_policy,
    verify_scholarship_eligibility,
    check_active_scholarship_status,
    verify_internship_semester_and_credits,
    verify_observation_course_enrolled,
    verify_semester_and_credits,
)

def validate_global_requirement(student_data):
    is_paid, msg = validate_payment_status(student_data)
    if not is_paid:
        raise serializers.ValidationError(msg)

    is_active, msg = validate_active_status(student_data)
    if not is_active:
        raise serializers.ValidationError(msg)

"""
    validasi rekomendasi beasiswa
"""
def validate_sr_bea(student_data, payload) :
    is_valid, msg = verify_scholarship_eligibility(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)

    is_valid, msg = check_active_scholarship_status(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)


"""
    validasi rekomendasi magang 
"""
def validate_sr_mag(student_data, payload):
    is_valid, msg = verify_internship_semester_and_credits(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)

    is_valid, msg = verify_ethical_policy(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)



def validate_sk_kul(student_data, payload):
    pass

def validate_sk_bea(student_data):
    is_valid, msg = check_active_scholarship_status(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)
    
def validate_sp_tra(student_data, student_courses_data, semester_aktif):
    is_enrolled, msg = check_semester_registration(student_data.get("nim"), student_courses_data, semester_aktif)
    if not is_enrolled:
        raise serializers.ValidationError(msg)
    
def validate_sp_skc(student_data):
    is_valid, msg = verify_ethical_policy(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)

def validate_sp_tas(student_data, payload):
    """Validator untuk Surat Pengantar Tugas Akhir (Biasanya min. semester 7 & 80 sks)"""
    # Sesuaikan parameter untuk Tugas Akhir: misal minimal semester 7 dan 120 SKS
    is_valid, msg = verify_semester_and_credits(student_data, minimum_sks=80, minimum_semester=7)
    if not is_valid:
        raise serializers.ValidationError(msg)

    is_valid, msg = verify_ethical_policy(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)

def validate_sp_mag(student_data, payload):
    """Validator untuk Surat Pengantar Magang (Min. semester 5 & 60 SKS)"""
    # Tetap menggunakan standar magang: semester 5 dan 80 SKS
    is_valid, msg = verify_semester_and_credits(student_data, minimum_sks=60, minimum_semester=5)
    if not is_valid:
        raise serializers.ValidationError(msg)
        
    is_valid, msg = verify_ethical_policy(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)
        

def validate_sr_mag(student_data, payload):
    is_valid, msg = verify_internship_semester_and_credits(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)

    is_valid, msg = verify_ethical_policy(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)

def validate_si_mag(student_data):
    is_valid, msg = verify_internship_semester_and_credits(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)

    is_valid, msg = verify_ethical_policy(student_data)
    if not is_valid:
        raise serializers.ValidationError(msg)

def validate_si_sur(student_data, payload, student_courses_data):
    student_nim = student_data.get("nim")
    # Ambil kode mata kuliah dari payload yang diinput mahasiswa/AI
    course_code = payload.get("kode_matkul") or payload.get("course_code", "")
    
    is_enrolled, msg = verify_observation_course_enrolled(student_nim, course_code, student_courses_data)
    if not is_enrolled:
        raise serializers.ValidationError(msg)
    
SURAT_VALIDATORS = {
    "SR-BEA": validate_sr_bea,
    "SR-MAG": validate_sr_mag,
    
    
    "SK-KUL": validate_sk_kul,
    "SK-BEA": validate_sk_bea,
    "SP-TRA": validate_sp_tra,
    "SP-SKC": validate_sp_skc,
    "SP-TAS": validate_sp_tas,
    "SP-MAG": validate_sp_mag,
    "SI-MAG": validate_si_mag,
    "SI-SUR": validate_si_sur,
}


def run_validators(kode_surat, student_data, payload, student_courses_data=None):
    """Fungsi orkestrator utama untuk menjalankan global dan specific check."""
    # 1. Jalankan validasi global terlebih dahulu
    validate_global_requirement(student_data)

    # 2. Jalankan validasi spesifik berdasarkan jenis surat
    validator_func = SURAT_VALIDATORS.get(kode_surat)
    if validator_func:
        # Cek apakah validator butuh parameter tambahan courses_data
        if kode_surat in ["SP-TRA", "SI-SUR"]:
            validator_func(student_data, payload, student_courses_data)
        else:
            validator_func(student_data, payload)