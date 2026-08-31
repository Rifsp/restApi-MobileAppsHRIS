from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class PushDeviceRegisterRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)
    push_token: str = Field(min_length=10, max_length=500)
    platform: str | None = Field(default=None, max_length=20)
    app_version: str | None = Field(default=None, max_length=20)

    @field_validator("device_id", "push_token")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Nilai tidak boleh kosong.")
        return value

    @field_validator("platform")
    @classmethod
    def normalize_platform(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if value not in {"android", "ios"}:
            raise ValueError("Platform harus android atau ios.")
        return value


class PushDeviceUnregisterRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)

    @field_validator("device_id")
    @classmethod
    def strip_device_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Device ID tidak boleh kosong.")
        return value


class PushDeviceData(BaseModel):
    i_push_device: int
    device_id: str
    platform: str | None = None
    app_version: str | None = None
    active: bool
    registered_at: datetime
    updated_at: datetime | None = None


class PushDeviceResponse(BaseModel):
    status: bool
    message: str
    data: PushDeviceData


class PushDeviceUnregisterData(BaseModel):
    device_id: str
    active: bool


class PushDeviceUnregisterResponse(BaseModel):
    status: bool
    message: str
    data: PushDeviceUnregisterData
