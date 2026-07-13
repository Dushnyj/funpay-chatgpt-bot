from __future__ import annotations

import enum
import re
from typing import Protocol, runtime_checkable


# 6 цифр, НЕ окружённых другими цифрами (отсекает phone numbers, order IDs)
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


class EmailErrorCode(str, enum.Enum):
    AUTH_FAILED = "email_auth_failed"
    NO_CODE = "email_code_not_found"
    UNSUPPORTED = "email_provider_unsupported"
    CONNECTION_FAILED = "email_connection_failed"
    SECURITY_CHALLENGE = "email_security_challenge"
    TIMEOUT = "email_timeout"


class EmailProviderError(RuntimeError):
    """Safe, stage-aware failure from an email verification provider."""

    def __init__(self, code: EmailErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@runtime_checkable
class EmailProvider(Protocol):
    """Абстракция источника email-кодов для подтверждения логина OpenAI."""

    async def preflight(self) -> None:
        """Проверяет доступ и запоминает уже существующие письма."""
        ...

    async def fetch_verification_code(self, timeout: float = 60.0) -> str | None:
        """Ждёт новый код OpenAI; при диагностируемом сбое поднимает ошибку."""
        ...


def parse_verification_code(text: str) -> str | None:
    """Извлекает 6-значный код подтверждения из текста письма.

    Ищет ровно 6 цифр, не окружённых другими цифрами.
    Возвращает первый match или None.
    """
    match = _CODE_RE.search(text)
    return match.group(1) if match else None
