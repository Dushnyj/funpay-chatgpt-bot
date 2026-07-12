import io

import qrcode
from app.integrations.email.qr_decode import decode_qr_secret


def _make_qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_decode_otpauth_uri():
    secret = "JBSWY3DPEHPK3PXP"
    uri = f"otpauth://totp/OpenAI:user@example.com?secret={secret}&issuer=OpenAI"
    png = _make_qr_png(uri)
    result = decode_qr_secret(png)
    assert result == secret


def test_decode_returns_none_for_non_qr_image():
    # Мусорные байты — не QR
    result = decode_qr_secret(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    assert result is None


def test_decode_returns_none_for_non_otpauth_qr():
    png = _make_qr_png("https://example.com")
    assert decode_qr_secret(png) is None


def test_decode_handles_empty_bytes():
    assert decode_qr_secret(b"") is None
