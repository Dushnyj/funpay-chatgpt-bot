import base64
import json
from datetime import datetime, timezone

import pytest

from app.integrations.openai.oauth import IdTokenClaims, parse_id_token


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
