from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str
    encryption_key: str
    secret_key: str
    admin_password_hash: str = ""
    admin_cookie_secure: bool = True
    admin_login_max_attempts: int = Field(default=5, ge=1, le=100)
    admin_login_window_seconds: int = Field(default=300, ge=1, le=86400)
    funpay_session_key: str = ""
    telegram_bot_token: str = ""
    telegram_seller_chat_id: str = ""

    @field_validator("encryption_key")
    @classmethod
    def _validate_fernet_key(cls, v: str) -> str:
        Fernet(v.encode())
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
