import base64
import io

import qrcode
from app.services.otp_export import generate_otpauth_uri, generate_qr_png, generate_qr_base64


def test_generate_otpauth_uri_format():
    uri = generate_otpauth_uri("JBSWY3DPEHPK3PXP", "user@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "OpenAI%3A" in uri  # issuer:account label, : кодируется как %3A
    assert "user%40example.com" in uri  # @ кодируется как %40
    assert "secret=JBSWY3DPEHPK3PXP" in uri
    assert "issuer=OpenAI" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_generate_otpauth_uri_custom_issuer():
    uri = generate_otpauth_uri("SECRET", "user", issuer="Custom")
    assert "Custom%3Auser" in uri
    assert "issuer=Custom" in uri


def test_generate_qr_png_valid_image():
    uri = generate_otpauth_uri("JBSWY3DPEHPK3PXP", "user@example.com")
    png = generate_qr_png(uri)
    assert isinstance(png, bytes)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature
    # Roundtrip: decode QR и проверим что получили тот же URI
    from pyzbar.pyzbar import decode
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    results = decode(img)
    assert len(results) == 1
    assert results[0].data.decode() == uri


def test_generate_qr_base64_data_url():
    uri = generate_otpauth_uri("SECRET", "user")
    data_url = generate_qr_base64(uri)
    assert data_url.startswith("data:image/png;base64,")
    # base64-часть декодируется в валидный PNG
    b64_part = data_url.split(",", 1)[1]
    png = base64.b64decode(b64_part)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
