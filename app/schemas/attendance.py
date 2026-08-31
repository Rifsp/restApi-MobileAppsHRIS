from pydantic import BaseModel


class ActiveDinasData(BaseModel):
    i_dinas: int
    i_dinas_id: str
    e_kota: str | None = None
    e_area: str | None = None
    e_remark: str | None = None
    d_berangkat: str
    d_kembali: str


class AttendanceContextData(BaseModel):
    tanggal: str
    has_active_dinas: bool
    active_dinas: ActiveDinasData | None = None


class AttendanceContextResponse(BaseModel):
    status: bool
    message: str
    data: AttendanceContextData


class CheckInData(BaseModel):
    i_absensi: int
    tanggal: str
    jam_in: str
    e_status: str
    menit_terlambat: int
    distance_meter: float | None = None
    radius_meter: int | None = None
    e_lokasi_name: str | None = None
    foto_in: str
    alamat_in: str
    e_timezone: str
    is_lk: bool
    i_dinas: int | None = None
    i_dinas_id: str | None = None
    e_kota: str | None = None


class CheckInResponse(BaseModel):
    status: bool
    message: str
    data: CheckInData


class CheckOutData(BaseModel):
    i_absensi: int
    tanggal: str
    jam_in: str
    jam_out: str
    e_status: str
    menit_terlambat: int
    menit_lembur: int
    distance_meter: float | None = None
    radius_meter: int | None = None
    e_lokasi_name: str | None = None
    foto_out: str
    alamat_out: str
    e_timezone: str
    is_lk: bool
    i_dinas: int | None = None
    i_dinas_id: str | None = None
    e_kota: str | None = None


class CheckOutResponse(BaseModel):
    status: bool
    message: str
    data: CheckOutData


class AttendanceHistoryItem(BaseModel):
    i_absensi: int
    tanggal: str
    jam_in: str | None = None
    jam_out: str | None = None
    e_status: str
    menit_terlambat: int
    menit_lembur: int
    metode_in: str | None = None
    metode_out: str | None = None
    e_lokasi_name: str | None = None
    alamat_in: str | None = None
    alamat_out: str | None = None
    foto_in: str | None = None
    foto_out: str | None = None
    e_timezone: str
    is_lk: bool
    i_dinas: int | None = None
    i_dinas_id: str | None = None
    e_kota: str | None = None


class AttendanceHistoryData(BaseModel):
    period: str
    page: int
    page_size: int
    total: int
    total_pages: int
    items: list[AttendanceHistoryItem]


class AttendanceHistoryResponse(BaseModel):
    status: bool
    message: str
    data: AttendanceHistoryData
