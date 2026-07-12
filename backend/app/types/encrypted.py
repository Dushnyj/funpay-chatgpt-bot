from cryptography.fernet import InvalidToken
from sqlalchemy import Dialect, String, TypeDecorator

from app.services.crypto import decrypt_legacy_layers, encrypt


class FernetEncrypted(TypeDecorator[str]):
    impl = String
    cache_ok = True

    def __init__(self, *args, allow_legacy_plaintext: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_legacy_plaintext = allow_legacy_plaintext

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        try:
            return decrypt_legacy_layers(value)
        except InvalidToken:
            # Selected settings columns historically stored plaintext. Never
            # treat a Fernet-looking value as legacy plaintext: that would hide
            # an incorrect ENCRYPTION_KEY and could corrupt the secret on save.
            if self.allow_legacy_plaintext and not value.startswith("gAAAA"):
                return value
            raise
