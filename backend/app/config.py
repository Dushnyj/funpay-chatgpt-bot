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
    secret_key: str = Field(min_length=32)
    admin_password_hash: str = ""
    admin_cookie_secure: bool = True
    admin_login_max_attempts: int = Field(default=5, ge=1, le=100)
    admin_login_window_seconds: int = Field(default=300, ge=1, le=86400)
    # Chromium is the dominant memory consumer. Keep one scheduled refresh or
    # revoke at a time on the documented 2 GiB host unless explicitly raised.
    browser_concurrency_cap: int = Field(default=1, ge=1, le=20)
    funpay_session_key: str = ""
    telegram_bot_token: str = ""
    telegram_seller_chat_id: str = ""
    microsoft_graph_client_id: str = ""
    microsoft_graph_client_secret: str = ""
    microsoft_graph_redirect_uri: str = ""

    @field_validator("encryption_key")
    @classmethod
    def _validate_fernet_key(cls, v: str) -> str:
        Fernet(v.encode())
        return v

    @field_validator("secret_key")
    @classmethod
    def _validate_secret_key(cls, value: str) -> str:
        if value.strip().casefold() in {
            "changeme",
            "change-me",
            "secret",
            "test-secret",
            "changeme-secret-key-for-jwt",
        }:
            raise ValueError("SECRET_KEY must be a unique random value")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
