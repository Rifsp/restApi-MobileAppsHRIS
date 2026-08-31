from datetime import date, datetime

from pydantic import BaseModel, Field


class AnnouncementItem(BaseModel):
    i_pengumuman: int
    e_judul: str
    e_isi: str
    d_mulai: date
    d_selesai: date | None = None
    d_publish: datetime | None = None
    is_read: bool
    d_baca: datetime | None = None


class AnnouncementListData(BaseModel):
    items: list[AnnouncementItem]
    page: int
    limit: int
    total: int
    unread: int


class AnnouncementListResponse(BaseModel):
    status: bool
    message: str
    data: AnnouncementListData


class AnnouncementDetailData(BaseModel):
    announcement: AnnouncementItem


class AnnouncementDetailResponse(BaseModel):
    status: bool
    message: str
    data: AnnouncementDetailData


class AnnouncementUnreadData(BaseModel):
    unread: int = Field(ge=0)


class AnnouncementUnreadResponse(BaseModel):
    status: bool
    message: str
    data: AnnouncementUnreadData


class AnnouncementReadData(BaseModel):
    i_pengumuman: int
    is_read: bool
    d_baca: datetime


class AnnouncementReadResponse(BaseModel):
    status: bool
    message: str
    data: AnnouncementReadData
