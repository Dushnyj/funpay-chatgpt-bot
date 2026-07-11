import pytest


def test_encrypt_decrypt_roundtrip():
    from app.services.crypto import decrypt, encrypt

    plaintext = "super-secret-totp-JBSWY3DPEHPK3PXP"
    ciphertext = encrypt(plaintext)
    assert ciphertext != plaintext
    assert decrypt(ciphertext) == plaintext


def test_decrypt_invalid_input_raises():
    from cryptography.fernet import InvalidToken

    from app.services.crypto import decrypt

    with pytest.raises(InvalidToken):
        decrypt("not-a-valid-token-aaaaa")
