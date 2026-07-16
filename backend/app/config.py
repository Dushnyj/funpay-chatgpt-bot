from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
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
    # Home relay control plane.  The SOCKS port is internal to the Compose
    # network; only the SSH enrollment endpoint is exposed by operations.
    home_relay_proxy_host: str = "home-relay"
    home_relay_proxy_port: int = Field(default=1080, ge=1, le=65535)
    home_relay_public_base_url: str = "https://funpay-bot.duckdns.org"
    home_relay_public_host: str = "funpay-bot.duckdns.org"
    home_relay_ssh_port: int = Field(default=2222, ge=1, le=65535)
    home_relay_ssh_user: str = "relay"
    home_relay_authorized_keys_path: str = (
        "/var/lib/funpay-relay/auth/authorized_keys"
    )
    home_relay_host_public_key_path: str = (
        "/var/lib/funpay-relay/auth/ssh_host_ed25519_key.pub"
    )
    home_relay_setup_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    home_relay_session_ack_timeout_seconds: float = Field(
        default=5.0, ge=0.1, le=30.0
    )
    # A route is sellable only while a recent end-to-end probe confirms it.
    # The TTL is deliberately longer than the probe interval so a single
    # transient probe failure is published explicitly instead of becoming a
    # silent direct-network fallback.
    proxy_route_probe_interval_seconds: int = Field(
        default=60, ge=30, le=600
    )
    proxy_route_max_age_seconds: int = Field(
        default=180, ge=60, le=3600
    )

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

    @model_validator(mode="after")
    def _validate_proxy_probe_timing(self) -> "Settings":
        if (
            self.proxy_route_max_age_seconds
            < self.proxy_route_probe_interval_seconds * 2
        ):
            raise ValueError(
                "PROXY_ROUTE_MAX_AGE_SECONDS must be at least twice "
                "PROXY_ROUTE_PROBE_INTERVAL_SECONDS"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
