from __future__ import annotations

import re
from typing import Protocol, runtime_checkable


# 6 цифр, НЕ окружённых другими цифрами (отсекает phone numbers, order IDs)
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


@runtime_checkable
class EmailProvider(Protocol):
    """Абстракция источника email-кодов для подтверждения логина OpenAI."""

    async def fetch_verification_code(self, timeout: float = 60.0) -> str | None:
        """Ждёт письмо с кодом от OpenAI, возвращает код или None при таймауте."""
        ...


def parse_verification_code(text: str) -> str | None:
    """Извлекает 6-значный код подтверждения из текста письма.

    Ищет ровно 6 цифр, не окружённых другими цифрами.
    Возвращает первый match или None.
    """
    match = _CODE_RE.search(text)
    return match.group(1) if match else None
