import pytest


def _setup_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("ENCRYPTION_KEY", _gen_key())
    monkeypatch.setenv("SECRET_KEY", "s")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "h")
    monkeypatch.setenv("FUNPAY_SESSION_KEY", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "")
    from app.config import get_settings
    get_settings.cache_clear()


def test_encrypt_decrypt_roundtrip(monkeypatch):
    _setup_env(monkeypatch)
    from app.services.crypto import decrypt, encrypt

    plaintext = "super-secret-totp-JBSWY3DPEHPK3PXP"
    ciphertext = encrypt(plaintext)
    assert ciphertext != plaintext
    assert decrypt(ciphertext) == plaintext


def test_decrypt_invalid_input_raises(monkeypatch):
    _setup_env(monkeypatch)
    from cryptography.fernet import InvalidToken
    from app.services.crypto import decrypt

    with pytest.raises(InvalidToken):
        decrypt("not-a-valid-token-aaaaa")


def _gen_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()
