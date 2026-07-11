from sqlalchemy import Dialect, String, TypeDecorator

from app.services.crypto import decrypt, encrypt


class FernetEncrypted(TypeDecorator[str]):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return decrypt(value)
