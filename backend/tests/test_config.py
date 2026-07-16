import pytest
from pydantic import ValidationError


def test_settings_loads_from_env():
    from app.config import Settings

    settings = Settings()
    assert "sqlite" in settings.database_url
    assert settings.encryption_key
    assert settings.secret_key == "test-secret-key-at-least-32-bytes-long"
    assert settings.browser_concurrency_cap == 1


def test_settings_validates_encryption_key(monkeypatch):
    # Переопределяем валидный ключ из autouse-фикстуры на невалидный
    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-valid-fernet-key")

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_validates_browser_concurrency_cap(monkeypatch):
    monkeypatch.setenv("BROWSER_CONCURRENCY_CAP", "0")

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_requires_proxy_ttl_to_cover_probe_jitter(monkeypatch):
    monkeypatch.setenv("PROXY_ROUTE_PROBE_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("PROXY_ROUTE_MAX_AGE_SECONDS", "180")

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()
