import pytest
from pydantic import ValidationError


def test_settings_loads_from_env():
    from app.config import Settings

    settings = Settings()
    assert "sqlite" in settings.database_url
    assert settings.encryption_key
    assert settings.secret_key == "test-secret-key-at-least-32-bytes-long"


def test_settings_validates_encryption_key(monkeypatch):
    # Переопределяем валидный ключ из autouse-фикстуры на невалидный
    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-valid-fernet-key")

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()
