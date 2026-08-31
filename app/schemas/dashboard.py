from datetime import date

from pydantic import BaseModel


class DashboardProfile(BaseModel):
    i_user: int
    i_karyawan: int
    i_user_id: str
    e_user_name: str
    e_karyawan_name: str
    ava: str | None = None

    i_company: int
    e_company_name: str

    e_nik: str | None = None
    e_tipe_karyawan: str | None = None
    f_wajib_lokasi: bool
    e_timezone: str


class DashboardSchedule(BaseModel):
    tanggal: date
    n_hari: int
    jam_masuk: str | None = None
    jam_keluar: str | None = None
    toleransi_menit: int
    f_libur: bool
    e_timezone: str


class DashboardLocation(BaseModel):
    i_lokasi: int
    e_lokasi_name: str
    latitude: float
    longitude: float
    radius_meter: int


class DashboardDinas(BaseModel):
    i_dinas: int
    i_dinas_id: str
    e_area: str | None = None
    e_kota: str | None = None
    e_remark: str | None = None
    d_berangkat: date
    d_kembali: date


class DashboardHoliday(BaseModel):
    d_libur: date
    e_keterangan: str
    f_nasional: bool


class DashboardAttendance(BaseModel):
    i_absensi: int | None = None
    jam_in: str | None = None
    jam_out: str | None = None
    metode_in: str | None = None
    metode_out: str | None = None
    e_status: str
    menit_terlambat: int
    menit_lembur: int
    is_lk: bool
    i_dinas: int | None = None
    i_dinas_id: str | None = None
    e_kota: str | None = None

    can_check_in: bool
    can_check_in_regular: bool
    can_check_in_lk: bool
    can_check_out: bool


class DashboardData(BaseModel):
    profile: DashboardProfile
    schedule: DashboardSchedule
    location: DashboardLocation | None = None
    active_dinas: DashboardDinas | None = None
    holiday: DashboardHoliday | None = None
    attendance_today: DashboardAttendance


class DashboardResponse(BaseModel):
    status: bool
    message: str
    data: DashboardData
