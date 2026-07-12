import base64
import json
from contextlib import asynccontextmanager

import pytest

from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier
from app.services.crypto import decrypt, encrypt


def _make_jwt(payload: dict) -> str:
    """Создаёт минимальный по структуре JWT (без подписи) для тестов."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


@pytest.mark.asyncio
async def test_validate_account_success(session, monkeypatch):
    """Первичная валидация: Playwright логин → токены → замер лимитов → аккаунт active."""
    from app.integrations.openai.oauth import RefreshedTokens
    from app.services.account_limits import MeasureResult
    from app.services.account_validation import ValidationOutcome, validate_account

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="new@e.com",
        password_encrypted=encrypt("pass123"),
        totp_secret_encrypted=encrypt("JBSWY3DPEHPK3PXP"),
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    # browser_context: не запускаем реальный Chromium — отдаём фейковый объект-заглушку
    @asynccontextmanager
    async def fake_browser_context(*args, **kw):
        yield object()

    # Мокаем Playwright-логин: возвращает tuple (auth_code, code_verifier)
    async def fake_login_and_get_auth_code(context, login, password, totp_secret, **kw):
        assert login == "new@e.com"
        assert password == "pass123"
        return "fake-auth-code", "fake-verifier"

    async def fake_exchange(code, verifier, redirect_uri):
        assert code == "fake-auth-code"
        assert verifier == "fake-verifier"
        return RefreshedTokens(
            access_token="initial-access",
            refresh_token="initial-refresh",
            id_token=_make_jwt({
                "email": "new@e.com",
                "https://api.openai.com/auth": {"plan_type": "plus"},
                "https://api.openai.com/account": {"account_id": "openai-acc-1"},
            }),
        )

    # Мокаем замер (чтобы не дёргать реальный backend-api)
    async def fake_measure(session_arg, account_id):
        assert account_id == acc.id
        return MeasureResult.OK

    monkeypatch.setattr("app.services.account_validation.browser_context", fake_browser_context)
    monkeypatch.setattr("app.services.account_validation.login_and_get_auth_code", fake_login_and_get_auth_code)
    monkeypatch.setattr("app.services.account_validation.exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr("app.services.account_validation.measure_account_limits", fake_measure)

    outcome = await validate_account(session, acc.id)
    assert outcome == ValidationOutcome.OK

    # Проверяем: аккаунт active, AccountLimits создан с токенами
    reloaded_acc = await session.get(Account, acc.id)
    assert reloaded_acc.status == "active"

    limits = await session.get(AccountLimits, acc.id)
    assert limits is not None
    assert decrypt(limits.refresh_token_encrypted) == "initial-refresh"
    assert limits.account_id_openai == "openai-acc-1"
    assert limits.refresh_status == "ok"


@pytest.mark.asyncio
async def test_validate_account_login_failure(session, monkeypatch):
    """Playwright логин не удался → аккаунт остаётся в pending_validation."""
    from app.integrations.playwright.oauth_login import OAuthLoginError
    from app.services.account_validation import ValidationOutcome, validate_account

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="bad@e.com",
        password_encrypted=encrypt("wrong"),
        totp_secret_encrypted=encrypt("t"),
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    # browser_context тоже нужно подменить, иначе запустится реальный Chromium
    @asynccontextmanager
    async def fake_browser_context(*args, **kw):
        yield object()

    async def failing_login(context, login, password, totp_secret, **kw):
        raise OAuthLoginError("invalid credentials")

    monkeypatch.setattr("app.services.account_validation.browser_context", fake_browser_context)
    monkeypatch.setattr("app.services.account_validation.login_and_get_auth_code", failing_login)

    outcome = await validate_account(session, acc.id)
    assert outcome == ValidationOutcome.LOGIN_FAILED

    reloaded = await session.get(Account, acc.id)
    assert reloaded.status == "pending_validation"
