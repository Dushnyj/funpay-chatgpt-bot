import base64
import json
from datetime import datetime, timezone

import pytest
import httpx

from app.integrations.openai.oauth import (
    IdTokenClaims,
    RefreshedTokens,
    exchange_code_for_tokens,
    openai_http_client,
    parse_id_token,
    refresh_access_token,
)
from app.integrations.playwright.proxy import BrowserProxy, ProxyUnavailableError


def _make_jwt(payload: dict) -> str:
    """Создаёт минимальный валидный по структуре JWT (без подписи)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


def test_parse_id_token_extracts_claims():
    jwt = _make_jwt({
        "email": "user@example.com",
        "https://api.openai.com/auth": {"plan_type": "plus"},
        "https://api.openai.com/profile": {"subscription_expires_at": 1723680000},
        "https://api.openai.com/account": {"account_id": "acc-xyz-123"},
    })
    claims = parse_id_token(jwt)
    assert claims.email == "user@example.com"
    assert claims.plan_type == "plus"
    assert claims.account_id == "acc-xyz-123"
    assert claims.subscription_expires_at is not None


def test_parse_current_access_token_claims_from_auth_and_profile_namespaces():
    jwt = _make_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-current",
            "chatgpt_plan_type": "free",
        },
        "https://api.openai.com/profile": {
            "email": "current@example.com",
        },
    })

    claims = parse_id_token(jwt)

    assert claims.email == "current@example.com"
    assert claims.plan_type == "free"
    assert claims.account_id == "acct-current"
    assert claims.subscription_expires_at is None


def test_current_token_claims_take_precedence_over_legacy_aliases():
    jwt = _make_jwt({
        "email": "legacy@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-current",
            "chatgpt_plan_type": "free",
            "plan_type": "plus",
        },
        "https://api.openai.com/account": {"account_id": "acct-legacy"},
        "https://api.openai.com/profile": {"email": "profile@example.com"},
    })

    claims = parse_id_token(jwt)

    assert claims.email == "legacy@example.com"
    assert claims.plan_type == "free"
    assert claims.account_id == "acct-current"


def test_parse_id_token_handles_missing_claims():
    jwt = _make_jwt({"email": "minimal@example.com"})
    claims = parse_id_token(jwt)
    assert claims.email == "minimal@example.com"
    assert claims.plan_type is None
    assert claims.account_id is None
    assert claims.subscription_expires_at is None


def test_parse_id_token_invalid_jwt_returns_empty_claims():
    claims = parse_id_token("not.a.valid.jwt.token")
    assert claims.email is None
    assert claims.plan_type is None


def test_id_token_claims_defaults():
    claims = IdTokenClaims()
    assert claims.email is None
    assert claims.plan_type is None
    assert claims.account_id is None
    assert claims.subscription_expires_at is None


@pytest.mark.asyncio
async def test_refresh_access_token_success(httpx_mock):
    from app.integrations.openai.oauth import refresh_access_token

    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        json={
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "id_token": _make_jwt({"email": "u@e.com"}),
        },
    )

    result = await refresh_access_token("old-refresh-token")
    assert result.access_token == "new-access-token"
    assert result.refresh_token == "new-refresh-token"
    assert result.id_token is not None


@pytest.mark.asyncio
async def test_refresh_access_token_keeps_old_refresh_if_missing(httpx_mock):
    from app.integrations.openai.oauth import refresh_access_token

    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        json={
            "access_token": "new-access",
            # refresh_token отсутствует — OpenAI иногда не возвращает его
        },
    )

    result = await refresh_access_token("original-refresh")
    assert result.access_token == "new-access"
    assert result.refresh_token == "original-refresh"  # fallback на старый


@pytest.mark.asyncio
async def test_refresh_access_token_raises_on_401(httpx_mock):
    from app.integrations.openai.exceptions import RefreshFailedError
    from app.integrations.openai.oauth import refresh_access_token

    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        status_code=401,
        text="invalid_grant",
    )

    with pytest.raises(RefreshFailedError):
        await refresh_access_token("expired-token")


def test_openai_http_client_configures_selected_socks_route(monkeypatch):
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_async_client(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("app.integrations.openai.oauth.httpx.AsyncClient", fake_async_client)
    selected = BrowserProxy(
        route_id=17,
        proxy_type="socks5",
        host="home-relay.internal",
        port=1080,
        username="relay-user",
        password="relay-password",
    )

    client = openai_http_client(proxy=selected, timeout=12.0)

    assert client is sentinel
    assert captured["trust_env"] is False
    assert captured["timeout"] == 12.0
    configured_proxy = captured["proxy"]
    assert isinstance(configured_proxy, httpx.Proxy)
    assert str(configured_proxy.url) == "socks5://home-relay.internal:1080"
    assert configured_proxy.auth == ("relay-user", "relay-password")


@pytest.mark.asyncio
async def test_exchange_code_uses_selected_proxy(monkeypatch):
    selected = BrowserProxy(18, "http", "proxy.internal", 3128)
    observed: list[BrowserProxy | None] = []

    class FakeResponse:
        is_success = True

        @staticmethod
        def json():
            return {"access_token": "access", "refresh_token": "refresh"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    def fake_client(*, proxy=None, timeout=30.0):
        observed.append(proxy)
        return FakeClient()

    monkeypatch.setattr("app.integrations.openai.oauth.openai_http_client", fake_client)

    result = await exchange_code_for_tokens(
        "authorization-code",
        "verifier",
        "http://localhost/callback",
        proxy=selected,
    )

    assert result == RefreshedTokens("access", "refresh", None)
    assert observed == [selected]


@pytest.mark.asyncio
async def test_refresh_proxy_transport_failure_is_secret_free_and_never_falls_back(
    monkeypatch,
):
    selected = BrowserProxy(
        19,
        "http",
        "proxy.internal",
        3128,
        username="relay-user",
        password="do-not-leak",
    )
    attempts = 0

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("transport included do-not-leak")

    def fake_client(*, proxy=None, timeout=30.0):
        assert proxy is selected
        return FailingClient()

    async def no_backoff(_attempt):
        return None

    monkeypatch.setattr("app.integrations.openai.oauth.openai_http_client", fake_client)
    monkeypatch.setattr("app.integrations.openai.oauth._short_backoff", no_backoff)

    with pytest.raises(ProxyUnavailableError) as failure:
        await refresh_access_token("refresh-secret", proxy=selected)

    assert attempts == 3
    assert "do-not-leak" not in str(failure.value)
    assert "refresh-secret" not in str(failure.value)
    assert failure.value.__cause__ is None
