import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone

# Константы из codex-switcher (реверс-инжиниринг OpenAI OAuth)
OPENAI_ISSUER = "https://auth.openai.com"
OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USER_AGENT = "codex-cli/1.0.0"

# Пространства имён claims в id_token OpenAI
_AUTH_NS = "https://api.openai.com/auth"
_PROFILE_NS = "https://api.openai.com/profile"
_ACCOUNT_NS = "https://api.openai.com/account"


@dataclass
class IdTokenClaims:
    email: str | None = None
    plan_type: str | None = None
    account_id: str | None = None
    subscription_expires_at: datetime | None = None


def parse_id_token(token: str) -> IdTokenClaims:
    """Парсит JWT id_token без проверки подписи — извлекает claims.

    Подпись не проверяем: токен получен напрямую от auth.openai.com по TLS,
    доверяем каналу. Проверка подписи добавила бы зависимость от публичного ключа,
    который OpenAI не документирует.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return IdTokenClaims()

    try:
        payload = _decode_jwt_part(parts[1])
    except Exception:
        return IdTokenClaims()

    return IdTokenClaims(
        email=payload.get("email"),
        plan_type=_get_nested(payload, _AUTH_NS, "plan_type"),
        account_id=_get_nested(payload, _ACCOUNT_NS, "account_id"),
        subscription_expires_at=_parse_unix(_get_nested(payload, _PROFILE_NS, "subscription_expires_at")),
    )


def _decode_jwt_part(part: str) -> dict:
    # JWT использует base64url без padding
    padded = part + "=" * (4 - len(part) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    return json.loads(decoded)


def _get_nested(payload: dict, namespace: str, key: str) -> str | int | None:
    nested = payload.get(namespace)
    if not isinstance(nested, dict):
        return None
    return nested.get(key)


def _parse_unix(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)
