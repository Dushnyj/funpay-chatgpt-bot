import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.integrations.openai.exceptions import RefreshFailedError
from app.integrations.playwright.proxy import BrowserProxy, ProxyUnavailableError

# Константы из codex-switcher (реверс-инжиниринг OpenAI OAuth)
OPENAI_ISSUER = "https://auth.openai.com"
OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USER_AGENT = "codex-cli/1.0.0"

# Пространства имён claims в JWT OpenAI.  Older tokens used ``plan_type`` in
# the auth namespace and kept the account ID in a separate namespace.  Current
# access tokens use ``chatgpt_plan_type`` and ``chatgpt_account_id`` in the auth
# namespace, while the profile namespace carries the email address.
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
    """Парсит OpenAI JWT без проверки подписи — извлекает claims.

    Имя функции оставлено для обратной совместимости, но парсер понимает как
    id_token старого формата, так и текущий access_token. Подпись не проверяем:
    токен получен напрямую от auth.openai.com по TLS,
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
        email=(
            _as_non_empty_string(payload.get("email"))
            or _as_non_empty_string(_get_nested(payload, _PROFILE_NS, "email"))
        ),
        plan_type=(
            _as_non_empty_string(
                _get_nested(payload, _AUTH_NS, "chatgpt_plan_type")
            )
            or _as_non_empty_string(_get_nested(payload, _AUTH_NS, "plan_type"))
        ),
        account_id=(
            _as_non_empty_string(
                _get_nested(payload, _AUTH_NS, "chatgpt_account_id")
            )
            or _as_non_empty_string(
                _get_nested(payload, _ACCOUNT_NS, "account_id")
            )
        ),
        subscription_expires_at=_parse_unix(_get_nested(payload, _PROFILE_NS, "subscription_expires_at")),
    )


def _decode_jwt_part(part: str) -> dict:
    # JWT использует base64url без padding
    padded = part + "=" * (4 - len(part) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    return json.loads(decoded)


def _get_nested(payload: dict, namespace: str, key: str) -> object | None:
    nested = payload.get(namespace)
    if not isinstance(nested, dict):
        return None
    return nested.get(key)


def _as_non_empty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _parse_unix(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class RefreshedTokens:
    access_token: str
    refresh_token: str
    id_token: str | None


def openai_http_client(
    *,
    proxy: BrowserProxy | None = None,
    timeout: float = 30.0,
) -> httpx.AsyncClient:
    """Build a secret-safe HTTP client for OpenAI authentication endpoints.

    A selected route is applied to the HTTP token requests as well as the
    Playwright browser. ``trust_env=False`` is deliberate: a configured route
    must never silently fall back to an unrelated process-level proxy.
    """

    client_proxy: httpx.Proxy | None = None
    if proxy is not None:
        auth = None
        if proxy.username is not None:
            auth = (proxy.username, proxy.password or "")
        client_proxy = httpx.Proxy(proxy.server, auth=auth)
    try:
        return httpx.AsyncClient(
            proxy=client_proxy,
            timeout=timeout,
            trust_env=False,
        )
    except ImportError:
        # HTTPX raises here when its optional SOCKS transport is absent. Keep
        # the dependency/configuration detail out of durable job diagnostics.
        if proxy is not None:
            raise ProxyUnavailableError() from None
        raise


async def refresh_access_token(
    refresh_token: str,
    *,
    proxy: BrowserProxy | None = None,
) -> RefreshedTokens:
    """Обновляет access_token через refresh_token.

    POST https://auth.openai.com/oauth/token
    Возвращает новые токены. При 401/400 — RefreshFailedError (нужен перезаход).
    """
    body = (
        f"grant_type=refresh_token"
        f"&refresh_token={refresh_token}"
        f"&client_id={OPENAI_CLIENT_ID}"
    )

    async with openai_http_client(proxy=proxy) as client:
        for attempt in range(1, 4):
            try:
                response = await client.post(
                    f"{OPENAI_ISSUER}/oauth/token",
                    content=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                break
            except httpx.TransportError:
                if attempt == 3:
                    if proxy is not None:
                        raise ProxyUnavailableError() from None
                    raise
                await _short_backoff(attempt)

    if not response.is_success:
        # Upstream bodies are intentionally omitted: they are not needed for
        # diagnosis and may echo account-specific authentication details.
        raise RefreshFailedError(f"refresh failed: {response.status_code}")

    data = response.json()
    return RefreshedTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or refresh_token,
        id_token=data.get("id_token"),
    )


async def _short_backoff(attempt: int) -> None:
    await asyncio.sleep(0.25 * attempt)


async def exchange_code_for_tokens(
    code: str,
    code_verifier: str,
    redirect_uri: str,
    *,
    proxy: BrowserProxy | None = None,
) -> RefreshedTokens:
    """Обменивает authorization_code на токены (первичный вход через Playwright OAuth).

    PKCE code_verifier должен совпадать с тем, что сгенерирован в login_and_get_auth_code.
    """
    body = (
        f"grant_type=authorization_code"
        f"&code={code}"
        f"&redirect_uri={redirect_uri}"
        f"&client_id={OPENAI_CLIENT_ID}"
        f"&code_verifier={code_verifier}"
    )

    try:
        async with openai_http_client(proxy=proxy) as client:
            response = await client.post(
                f"{OPENAI_ISSUER}/oauth/token",
                content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.TransportError:
        if proxy is not None:
            raise ProxyUnavailableError() from None
        raise

    if not response.is_success:
        raise RefreshFailedError(
            f"token exchange failed: {response.status_code}"
        )

    data = response.json()
    return RefreshedTokens(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        id_token=data.get("id_token"),
    )
