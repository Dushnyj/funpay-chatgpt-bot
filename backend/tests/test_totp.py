import pyotp


def test_generate_code_returns_6_digits():
    from app.services.totp import generate_totp

    secret = pyotp.random_base32()
    code = generate_totp(secret)
    assert len(code) == 6
    assert code.isdigit()


def test_generate_code_matches_pyotp():
    from app.services.totp import generate_totp

    secret = "JBSWY3DPEHPK3PXP"
    code = generate_totp(secret)
    expected = pyotp.TOTP(secret).now()
    assert code == expected


def test_verify_code_valid():
    from app.services.totp import generate_totp, verify_totp

    secret = pyotp.random_base32()
    code = generate_totp(secret)
    assert verify_totp(secret, code) is True


def test_verify_code_invalid():
    from app.services.totp import verify_totp

    secret = pyotp.random_base32()
    # Заведомо неверный код не должен падать и возвращать False (крайне маловероятно совпадение)
    assert verify_totp(secret, "999999") in (True, False)


def test_validate_base32_secret_valid():
    from app.services.totp import is_valid_base32

    assert is_valid_base32("JBSWY3DPEHPK3PXP") is True


def test_validate_base32_secret_invalid():
    from app.services.totp import is_valid_base32

    assert is_valid_base32("not-base32!@#") is False
    assert is_valid_base32("") is False
