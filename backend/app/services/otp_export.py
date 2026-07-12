from __future__ import annotations

import base64
import io
from urllib.parse import quote

import qrcode


def generate_otpauth_uri(secret: str, account_name: str, issuer: str = "OpenAI") -> str:
    """Собирает otpauth:// URI для импорта в приложение-аутентификатор.

    Формат: otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30
    """
    label = f"{issuer}:{account_name}"
    return (
        f"otpauth://totp/{quote(label, safe='')}?"
        f"secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits=6&period=30"
    )


def generate_qr_png(uri: str) -> bytes:
    """Генерирует PNG QR-код из otpauth:// URI. Возвращает байты PNG."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_qr_base64(uri: str) -> str:
    """Генерирует QR-код и возвращает как base64 data URL (для <img src='...'>)."""
    png = generate_qr_png(uri)
    b64 = base64.b64encode(png).decode()
    return f"data:image/png;base64,{b64}"
