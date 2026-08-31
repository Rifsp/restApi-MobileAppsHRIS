from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "HRIS API"
    app_env: str = "development"

    db_host: str
    db_port: int = 5432
    db_name: str
    db_user: str
    db_password: str

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    jwt_issuer: str = "hris-api"
    jwt_audience: str = "hris-mobile"

    # Digunakan ERP/server internal untuk memicu pengiriman push.
    internal_api_key: str | None = Field(default=None, min_length=32)
    expo_push_access_token: str | None = None

    legacy_password_key: str
    legacy_password_iv: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

