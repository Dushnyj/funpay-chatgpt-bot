from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import get_settings

_ALGORITHM = "HS256"
_TOKEN_TTL = timedelta(hours=24)
_COOKIE_NAME = "access_token"

COOKIE_NAME = _COOKIE_NAME


@dataclass(frozen=True)
class AccessTokenClaims:
    subject: str
    session_version: int


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (TypeError, ValueError):
        # Corrupt or unsupported stored hashes are authentication failures,
        # not reasons for the login endpoint to return an internal error.
        return False


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(
        plain.encode("utf-8"), bcrypt.gensalt(),
    ).decode("ascii")


def create_access_token(subject: str = "admin", *, session_version: int = 0) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + _TOKEN_TTL).timestamp()),
        "sv": session_version,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> str | None:
    claims = decode_access_token_claims(token)
    return claims.subject if claims is not None else None


def decode_access_token_claims(token: str) -> AccessTokenClaims | None:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[_ALGORITHM])
        subject = payload.get("sub")
        session_version = payload.get("sv", 0)
        if not isinstance(subject, str) or not isinstance(session_version, int):
            return None
        return AccessTokenClaims(subject=subject, session_version=session_version)
    except jwt.PyJWTError:
        return None
