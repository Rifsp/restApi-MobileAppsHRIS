from datetime import date, datetime

from pydantic import BaseModel


class LeaveTypeItem(BaseModel):
    i_jenis_izin: int
    i_jenis_id: str
    e_jenis_name: str
    max_hari: int
    f_butuh_dokumen: bool
    f_potong_cuti: bool


class LeaveTypesResponse(BaseModel):
    status: bool
    message: str
    data: list[LeaveTypeItem]


class LeaveQuotaData(BaseModel):
    i_tahun: int
    n_jatah: int
    n_diambil: int
    n_sisa: int
    d_expired: date | None = None


class LeaveQuotaResponse(BaseModel):
    status: bool
    message: str
    data: LeaveQuotaData | None = None


class LeaveHistoryItem(BaseModel):
    i_pengajuan: int
    i_pengajuan_id: str
    i_jenis_izin: int
    i_jenis_id: str
    e_jenis_name: str
    d_pengajuan: date
    d_mulai: date
    d_selesai: date
    n_hari: int
    e_alasan: str
    e_lampiran: str | None = None
    i_status_dn: int
    e_status: str
    f_reject: bool
    e_reject_reason: str | None = None
    f_cancel: bool
    e_cancel_reason: str | None = None
    can_cancel: bool
    d_entry: datetime


class LeaveHistoryData(BaseModel):
    year: int
    page: int
    page_size: int
    total: int
    total_pages: int
    items: list[LeaveHistoryItem]


class LeaveHistoryResponse(BaseModel):
    status: bool
    message: str
    data: LeaveHistoryData


class LeaveApplyData(BaseModel):
    i_pengajuan: int
    i_pengajuan_id: str
    i_status_dn: int
    e_status: str
    n_hari: int
    e_lampiran: str | None = None


class LeaveApplyResponse(BaseModel):
    status: bool
    message: str
    data: LeaveApplyData


class LeaveCancelData(BaseModel):
    i_pengajuan: int
    i_pengajuan_id: str
    f_cancel: bool


class LeaveCancelResponse(BaseModel):
    status: bool
    message: str
    data: LeaveCancelData
