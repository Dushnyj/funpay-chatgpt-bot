from __future__ import annotations

import io
import logging
from urllib.parse import parse_qs, urlparse

from PIL import Image
from pyzbar.pyzbar import decode

logger = logging.getLogger(__name__)


def decode_qr_secret(image_bytes: bytes) -> str | None:
    """Декодирует QR-код из PNG/JPEG байтов, извлекает TOTP secret из otpauth:// URI.

    Возвращает base32 secret или None, если:
    - изображение не содержит QR
    - QR не содержит otpauth:// URI
    - в URI нет параметра secret
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception:
        return None

    try:
        results = decode(img)
    except Exception:
        logger.debug("pyzbar decode failed", exc_info=True)
        return None

    for result in results:
        data = result.data.decode("utf-8", errors="ignore") if isinstance(result.data, bytes) else result.data
        secret = _extract_secret_from_otpauth(data)
        if secret:
            return secret
    return None


def _extract_secret_from_otpauth(data: str) -> str | None:
    """Парсит otpauth:// URI и возвращает secret-параметр."""
    if not data.startswith("otpauth://"):
        return None
    try:
        parsed = urlparse(data)
        params = parse_qs(parsed.query)
        secrets = params.get("secret", [])
        return secrets[0] if secrets else None
    except Exception:
        return None
