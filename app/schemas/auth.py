from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(
        min_length=1,
        max_length=100,
    )
    password: str = Field(
        min_length=1,
        max_length=500,
    )
    device_id: str | None = Field(default=None, max_length=200)
    device_name: str | None = Field(default=None, max_length=200)
    platform: str | None = Field(default=None, max_length=20)
    app_version: str | None = Field(default=None, max_length=20)

class UserResponse(BaseModel):
    i_user: int
    i_karyawan: int
    i_user_id: str
    e_user_name: str
    e_karyawan_name: str
    f_pusat: bool | None = None
    ava: str | None = None
    i_company: int
    e_company_name: str
    e_nik: str | None = None
    e_tipe_karyawan: str | None = None
    f_wajib_lokasi: bool = False
    i_jadwal: int | None = None
    i_lokasi_absen: int | None = None
    e_timezone: str


class LoginData(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_in: int
    user: UserResponse


class LoginResponse(BaseModel):
    status: bool
    message: str
    data: LoginData


class CurrentUserData(BaseModel):
    user: UserResponse


class CurrentUserResponse(BaseModel):
    status: bool
    message: str
    data: CurrentUserData


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=500)


class RefreshData(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_in: int


class RefreshResponse(BaseModel):
    status: bool
    message: str
    data: RefreshData


class LogoutResponse(BaseModel):
    status: bool
    message: str
