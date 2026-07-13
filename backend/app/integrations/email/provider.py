from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


# 6 цифр, НЕ окружённых другими цифрами (отсекает phone numbers, order IDs)
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CODE_CONTEXT_RE = re.compile(
    r"(?:"
    r"(?:verification|security|login|sign[ -]?in|one[ -]?time|otp|"
    r"проверочн\w*|одноразов\w*|вход\w*)\s+"
    r")?"
    r"(?:verification\s+|security\s+|проверочн\w*\s+)?"
    r"(?:code|код)\D{0,32}(?<!\d)(\d{6})(?!\d)",
    re.IGNORECASE,
)
_CODE_CONTEXT_AFTER_RE = re.compile(
    r"(?<!\d)(\d{6})(?!\d)\s*(?:is|[-—:])\s*(?:your\s+)?"
    r"(?:(?:verification|security|login|sign[ -]?in|one[ -]?time|otp)\s+)?"
    r"(?:code|код)",
    re.IGNORECASE,
)


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


@dataclass(frozen=True, slots=True)
class FreshVerificationCode:
    """A mailbox code whose arrival time and message identity are proven.

    ``fingerprint`` identifies the mailbox message, not the six-digit secret.
    Callers may persist the fingerprint for deduplication; ``code`` must only
    be used for the immediate buyer response.
    """

    code: str
    received_at: datetime
    fingerprint: str


@runtime_checkable
class EmailProvider(Protocol):
    """Абстракция источника email-кодов для подтверждения логина OpenAI."""

    async def preflight(self) -> None:
        """Проверяет доступ и запоминает уже существующие письма."""
        ...

    async def fetch_verification_code(self, timeout: float = 60.0) -> str | None:
        """Ждёт новый код OpenAI; при диагностируемом сбое поднимает ошибку."""
        ...

    async def fetch_fresh_verification_code(
        self,
        *,
        not_before: datetime,
        timeout: float = 10.0,
    ) -> FreshVerificationCode:
        """Read a provably fresh existing code without taking a new baseline."""
        ...


def parse_verification_code(text: str) -> str | None:
    """Извлекает 6-значный код подтверждения из текста письма.

    Контекст ``verification/security/code`` имеет приоритет. Если письмо
    содержит несколько разных шестизначных чисел без однозначного контекста,
    функция fail-closed и ничего не угадывает.
    """
    normalized = re.sub(r"<[^>]+>", " ", text)
    all_codes = list(dict.fromkeys(_CODE_RE.findall(normalized)))
    if not all_codes:
        return None

    contextual = list(
        dict.fromkeys(
            [*_CODE_CONTEXT_RE.findall(normalized), *_CODE_CONTEXT_AFTER_RE.findall(normalized)]
        )
    )
    if len(contextual) == 1:
        return contextual[0]
    if len(contextual) > 1:
        return None
    return all_codes[0] if len(all_codes) == 1 else None
