import json
from datetime import datetime

"""
    global helper functions for validating student data and eligibility for various academic and administrative processes.
"""
def validate_payment_status(student_data):
    """
    Helper untuk memvalidasi status pembayaran UKT mahasiswa.
    Syarat: Status pembayaran harus 'LUNAS'.
    """
    status_ukt = student_data.get("status_pembayaran_ukt", "").upper()
    if status_ukt == "LUNAS":
        return True, "Pembayaran UKT valid (Lunas)."
    return False, "Pengajuan ditolak: Pembayaran UKT belum lunas."

def validate_active_status(student_data):
    """
    Helper untuk memvalidasi status keaktifan akademik mahasiswa.
    Syarat: Status akademik harus 'AKTIF'.
    """
    status_akademik = student_data.get("status_akademik", "").upper()
    if status_akademik == "AKTIF":
        return True, "Status akademik mahasiswa aktif."
    return False, "Pengajuan ditolak: Mahasiswa tidak sedang aktif (Cuti/Non-Aktif)."

"""
    surat rekomendasi magang
"""
def verify_ethical_policy(student_data):
    """
    Helper untuk memvalidasi apakah mahasiswa bersih dari masalah kedisiplinan 
    atau pelanggaran etik kampus.
    """
    ada_pelanggaran = student_data.get("catatan_pelanggaran_etik", False)
    status_disiplin = student_data.get("status_disiplin_kampus", True)

    if ada_pelanggaran or not status_disiplin:
        return False, "Pengajuan ditolak: Mahasiswa memiliki catatan pelanggaran etik atau masalah kedisiplinan."
    
    return True, "Kelayakan transkrip tervalidasi."

def verify_internship_semester_and_credits(student_data, minimum_sks=60):
    """
    Helper untuk memvalidasi apakah mahasiswa sudah memenuhi syarat SKS minimum 
    dan berada di semester yang diperbolehkan untuk mengambil magang.
    """
    total_sks_lulus = student_data.get("total_sks_lulus", 0)
    semester_aktif = student_data.get("semester_aktif", 1)

    # Contoh aturan: Magang biasanya boleh diambil mulai semester 5 dengan minimal 80 SKS lulus
    if semester_aktif < 5:
        return False, f"Pengajuan ditolak: Semester aktif ({semester_aktif}) belum memenuhi syarat minimum (Semester 5)."
    
    if total_sks_lulus < minimum_sks:
        return False, f"Pengajuan ditolak: Total SKS lulus ({total_sks_lulus}) belummencapai syarat minimum ({minimum_sks} SKS)."

    return True, "Syarat semester dan SKS magang terpenuhi."

"""
    surat rekomendasi beasiswa 
"""
def verify_scholarship_eligibility(student_data, minimum_gpa=3.25):
    """
    Helper untuk memvalidasi syarat akademik (IPK minimum) 
    dan kebersihan rekam jejak etik mahasiswa untuk beasiswa.
    """
    ipk = student_data.get("ipk_kumulatif", 0.00)
    ada_pelanggaran = student_data.get("catatan_pelanggaran_etik", False)

    if ipk < minimum_gpa:
        return False, f"Pengajuan ditolak: IPK ({ipk}) belum memenuhi syarat minimum ({minimum_gpa})."
    
    if ada_pelanggaran:
        return False, "Pengajuan ditolak: Mahasiswa memiliki catatan pelanggaran etik."

    return True, "Memenuhi syarat akademik dan kedisiplinan beasiswa."

def check_active_scholarship_status(student_data):
    """
    Helper untuk mengecek apakah mahasiswa sedang menerima beasiswa lain 
    (bergantung pada ketentuan jenis beasiswa yang dilamar).
    """
    is_beasiswa_active = student_data.get("status_beasiswa_aktif", False)
    if is_beasiswa_active:
        nama_beasiswa = student_data.get("nama_beasiswa_aktif", "Lainnya")
        return False, f"Peringatan/Ditolak: Mahasiswa sudah aktif menerima beasiswa ({nama_beasiswa})."
    
    return True, "Belum ada beasiswa aktif yang terdaftar."

 
   
def check_semester_registration(student_nim, student_courses_data, semester_aktif):
    """
    Helper untuk memvalidasi apakah mahasiswa sudah mengisi KRS 
    dan mengambil mata kuliah di semester aktifnya.
    """
    enrolled_courses = [
        sc for sc in student_courses_data 
        if sc["nim"] == student_nim 
        and sc["semester_taken"] == semester_aktif 
        and sc["status_krs"] == "AMBIL"
    ]
    
    if len(enrolled_courses) > 0:
        return True, f"Valid: Terdaftar pada {len(enrolled_courses)} mata kuliah semester aktif."
    return False, "Pengajuan ditolak: Mahasiswa belum melakukan pengisian KRS semester aktif."



def verify_observation_course_enrolled(student_nim, course_code, student_courses_data):
    """
    Helper untuk memvalidasi apakah mahasiswa terdaftar pada mata kuliah 
    yang memerlukan kegiatan observasi di semester berjalan.
    """
    enrolled = [
        sc for sc in student_courses_data
        if sc["nim"] == student_nim 
        and sc["kode_matkul"] == course_code 
        and sc["status_krs"] == "AMBIL"
    ]
    
    if enrolled:
        return True, "Valid: Mahasiswa terdaftar aktif pada mata kuliah terkait."
    return False, f"Pengajuan ditolak: Mahasiswa tidak terdaftar pada mata kuliah dengan kode '{course_code}' di semester ini."

def verify_semester_and_credits(student_data, minimum_sks=80, minimum_semester=5):
    """
    Helper universal untuk memvalidasi syarat semester minimum dan SKS minimum 
    (bisa digunakan untuk Magang, Tugas Akhir, dll dengan mengubah parameternya).
    """
    total_sks_lulus = student_data.get("total_sks_lulus", 0)
    semester_aktif = student_data.get("semester_aktif", 1)

    if semester_aktif < minimum_semester:
        return False, f"Pengajuan ditolak: Semester aktif Anda ({semester_aktif}) belum memenuhi syarat minimum (Semester {minimum_semester})."
    
    if total_sks_lulus < minimum_sks:
        return False, f"Pengajuan ditolak: Total SKS lulus ({total_sks_lulus}) belum mencapai syarat minimum ({minimum_sks} SKS)."

    return True, "Syarat semester dan SKS terpenuhi."