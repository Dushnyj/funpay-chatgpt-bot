from sqlalchemy import String, TypeDecorator

from app.services.crypto import decrypt, encrypt


class FernetEncrypted(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt(value)
