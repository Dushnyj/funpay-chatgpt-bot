from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _get_fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def decrypt_legacy_layers(ciphertext: str, *, max_layers: int = 4) -> str:
    """Decrypt a database value and unwrap historical double encryption.

    ``FernetEncrypted`` owns encryption at the ORM boundary. Older API/service
    callers encrypted values before assigning them to decorated columns, so a
    row could contain two (or more) valid Fernet layers. The first layer must
    always be valid; subsequent layers are removed only when they authenticate
    with the same key.
    """
    plaintext = decrypt(ciphertext)
    for _ in range(max_layers - 1):
        try:
            plaintext = decrypt(plaintext)
        except (InvalidToken, ValueError, UnicodeError):
            break
    return plaintext
