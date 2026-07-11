import pytest


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
    monkeypatch.setenv("ENCRYPTION_KEY", _gen_key())
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$dummyhash")
    monkeypatch.setenv("FUNPAY_SESSION_KEY", "golden-key-123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "")

    from app.config import Settings, get_settings
    get_settings.cache_clear()

    settings = Settings()
    assert "sqlite" in settings.database_url
    assert settings.encryption_key
    assert settings.secret_key == "test-secret"


def test_settings_validates_encryption_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-valid-fernet-key")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$dummyhash")
    monkeypatch.setenv("FUNPAY_SESSION_KEY", "key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "")

    from app.config import Settings, get_settings
    get_settings.cache_clear()

    with pytest.raises(Exception):
        Settings()


def _gen_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()
