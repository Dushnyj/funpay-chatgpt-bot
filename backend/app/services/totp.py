import pyotp


def generate_totp(secret: str) -> str:
    """Генерирует текущий 6-значный TOTP-код."""
    return pyotp.TOTP(secret).now()


def generate_totp_at(secret: str, timestamp: float) -> str:
    """Генерирует TOTP для фиксированного момента времени."""
    return pyotp.TOTP(secret).at(timestamp)


def verify_totp(secret: str, code: str) -> bool:
    """Проверяет код с допуском ±1 окно (30с) — покрывает рассинхрон часов."""
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def is_valid_base32(secret: str) -> bool:
    """Проверяет, что строка — валидный base32-секрет TOTP."""
    if not secret:
        return False
    try:
        pyotp.TOTP(secret).now()
        return True
    except Exception:
        return False
