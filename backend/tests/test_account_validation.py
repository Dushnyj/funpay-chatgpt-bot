import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.account import Account, AccountLimits, EmailOAuthCredential
from app.models.catalog import SubscriptionTier


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
        password_encrypted="pass123",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        email="new@gmail.com",
        email_password_encrypted="mailpass",
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    # browser_context: не запускаем реальный Chromium — отдаём фейковый объект-заглушку
    @asynccontextmanager
    async def fake_browser_context(*args, **kw):
        yield object()

    provider = MagicMock()
    provider.preflight = AsyncMock(
        side_effect=RuntimeError("optional IMAP is unavailable")
    )

    # Мокаем Playwright-логин: возвращает tuple (auth_code, code_verifier)
    async def fake_login_and_get_auth_code(context, login, password, totp_secret, **kw):
        assert login == "new@e.com"
        assert password == "pass123"
        assert kw["email_provider"] is provider
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
                "https://api.openai.com/profile": {
                    "subscription_expires_at": 1786752000,
                },
            }),
        )

    # Мокаем замер (чтобы не дёргать реальный backend-api)
    async def fake_measure(session_arg, account_id, **kwargs):
        assert account_id == acc.id
        assert kwargs["claim_plan_type"] == "plus"
        return MeasureResult.OK

    monkeypatch.setattr("app.services.account_validation.browser_context", fake_browser_context)
    monkeypatch.setattr(
        "app.services.account_validation.detect_imap_provider",
        lambda *_args, **_kwargs: provider,
    )
    monkeypatch.setattr("app.services.account_validation.login_and_get_auth_code", fake_login_and_get_auth_code)
    monkeypatch.setattr("app.services.account_validation.exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr("app.services.account_validation.measure_account_limits", fake_measure)

    outcome = await validate_account(session, acc.id)
    assert outcome == ValidationOutcome.OK
    provider.preflight.assert_not_awaited()

    # Проверяем: аккаунт active, AccountLimits создан с токенами
    reloaded_acc = await session.get(Account, acc.id)
    assert reloaded_acc.status == "active"
    assert reloaded_acc.chatgpt_last_check_at is not None
    assert reloaded_acc.subscription_expiry_source == "id_token"
    assert reloaded_acc.subscription_expires_at is not None

    limits = await session.get(AccountLimits, acc.id)
    assert limits is not None
    assert limits.refresh_token_encrypted == "initial-refresh"
    assert limits.account_id_openai == "openai-acc-1"
    assert limits.refresh_status == "ok"
    assert limits.subscription_expiry_source == "id_token"
    limits_expiry = limits.subscription_expires_at
    if limits_expiry is not None and limits_expiry.tzinfo is None:
        from datetime import timezone

        limits_expiry = limits_expiry.replace(tzinfo=timezone.utc)
    assert limits_expiry == reloaded_acc.subscription_expires_at


@pytest.mark.asyncio
async def test_token_identity_must_match_configured_account(session, monkeypatch):
    from app.integrations.openai.oauth import RefreshedTokens
    from app.services.account_validation import (
        AccountValidationError,
        ValidationCode,
        _save_tokens_and_measure,
    )

    account = Account(
        login="expected@example.com",
        email="mailbox@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        status="pending_validation",
    )
    session.add(account)
    await session.flush()
    measure = AsyncMock()
    monkeypatch.setattr(
        "app.services.account_validation.measure_account_limits",
        measure,
    )
    tokens = RefreshedTokens(
        access_token="access",
        refresh_token="refresh",
        id_token=_make_jwt({
            "email": "different@example.com",
            "https://api.openai.com/account": {"account_id": "wrong-account"},
        }),
    )

    with pytest.raises(AccountValidationError) as error:
        await _save_tokens_and_measure(session, account, tokens)

    assert error.value.code == ValidationCode.INVALID_CREDENTIALS.value
    assert await session.get(AccountLimits, account.id) is None
    measure.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_account_login_failure(session, monkeypatch):
    """A typed login failure is exposed and leaves a terminal account state."""
    from app.integrations.playwright.oauth_login import OAuthErrorCode, OAuthLoginError
    from app.services.account_validation import AccountValidationError, validate_account

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="bad@e.com",
        password_encrypted="wrong",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
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
        raise OAuthLoginError(
            "OpenAI отклонил логин или пароль.",
            code=OAuthErrorCode.INVALID_CREDENTIALS,
            stage="password",
        )

    monkeypatch.setattr("app.services.account_validation.browser_context", fake_browser_context)
    monkeypatch.setattr("app.services.account_validation.login_and_get_auth_code", failing_login)

    with pytest.raises(AccountValidationError) as error:
        await validate_account(session, acc.id)
    assert error.value.code == "invalid_credentials"
    assert error.value.stage == "password"
    assert "wrong" not in error.value.to_json()

    reloaded = await session.get(Account, acc.id)
    assert reloaded.status == "validation_failed"


@pytest.mark.asyncio
async def test_validate_account_no_totp_with_email_enables_2fa(session, monkeypatch):
    """Нет TOTP, но есть email → вызывается enable_2fa (mocked) → обычная валидация."""
    from app.integrations.openai.oauth import RefreshedTokens
    from app.services.account_limits import MeasureResult
    from app.services.account_validation import ValidationOutcome, validate_account

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="auto2fa@e.com",
        password_encrypted="pass123",
        totp_secret_encrypted="",
        email="auto2fa@gmail.com",
        email_password_encrypted="mailpass",
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    @asynccontextmanager
    async def fake_browser_context(*args, **kw):
        yield object()

    async def fake_enable_2fa(context, login, password, email_provider, **kw):
        assert login == "auto2fa@e.com"
        assert password == "pass123"
        assert email_provider is not None
        return "JBSWY3DPEHPK3PXP"

    async def fake_login_and_get_auth_code(context, login, password, totp_secret, **kw):
        assert totp_secret == "JBSWY3DPEHPK3PXP"
        return "fake-auth-code", "fake-verifier"

    async def fake_exchange(code, verifier, redirect_uri):
        return RefreshedTokens(
            access_token="initial-access",
            refresh_token="initial-refresh",
            id_token=_make_jwt({
                "email": "auto2fa@e.com",
                "https://api.openai.com/auth": {"plan_type": "plus"},
                "https://api.openai.com/account": {"account_id": "openai-acc-2"},
            }),
        )

    async def fake_measure(session_arg, account_id, **kwargs):
        assert account_id == acc.id
        assert kwargs["claim_plan_type"] == "plus"
        return MeasureResult.OK

    monkeypatch.setattr("app.services.account_validation.browser_context", fake_browser_context)
    provider = MagicMock()
    provider.preflight = AsyncMock()
    monkeypatch.setattr(
        "app.services.account_validation.detect_imap_provider",
        lambda *_args, **_kwargs: provider,
    )
    monkeypatch.setattr("app.services.account_validation.enable_2fa", fake_enable_2fa)
    monkeypatch.setattr("app.services.account_validation.login_and_get_auth_code", fake_login_and_get_auth_code)
    monkeypatch.setattr("app.services.account_validation.exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr("app.services.account_validation.measure_account_limits", fake_measure)

    outcome = await validate_account(session, acc.id)
    assert outcome == ValidationOutcome.OK

    reloaded_acc = await session.get(Account, acc.id)
    assert reloaded_acc.status == "active"
    # Секрет 2FA сохранён на аккаунте
    assert reloaded_acc.totp_secret_encrypted == "JBSWY3DPEHPK3PXP"

    limits = await session.get(AccountLimits, acc.id)
    assert limits is not None
    assert limits.refresh_token_encrypted == "initial-refresh"
    assert limits.account_id_openai == "openai-acc-2"


@pytest.mark.asyncio
async def test_new_totp_secret_survives_cancellation_before_second_login(
    session, monkeypatch
):
    import asyncio

    from app.services.account_validation import validate_account

    account = Account(
        login="cancelled@example.com",
        password_encrypted="password",
        totp_secret_encrypted="",
        email="cancelled@gmail.com",
        email_password_encrypted="mailpass",
        tier_id=None,
        status="pending_validation",
    )
    session.add(account)
    await session.commit()
    account_id = account.id

    provider = MagicMock()
    provider.preflight = AsyncMock()

    @asynccontextmanager
    async def fake_browser_context(*_args, **_kwargs):
        yield object()

    async def fake_enable(*_args, **_kwargs):
        return "JBSWY3DPEHPK3PXP"

    async def cancelled_login(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "app.services.account_validation.detect_imap_provider", lambda *_: provider
    )
    monkeypatch.setattr(
        "app.services.account_validation.browser_context", fake_browser_context
    )
    monkeypatch.setattr("app.services.account_validation.enable_2fa", fake_enable)
    monkeypatch.setattr(
        "app.services.account_validation.login_and_get_auth_code", cancelled_login
    )

    with pytest.raises(asyncio.CancelledError):
        await validate_account(session, account_id)
    await session.rollback()
    session.expire_all()

    reloaded = await session.get(Account, account_id)
    assert reloaded is not None
    assert reloaded.totp_secret_encrypted == "JBSWY3DPEHPK3PXP"


@pytest.mark.asyncio
async def test_validate_account_no_totp_no_email_invalid_2fa(session):
    """No TOTP/email produces a precise, terminal validation failure."""
    from app.services.account_validation import AccountValidationError, validate_account

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="nothing@e.com",
        password_encrypted="pass123",
        totp_secret_encrypted="",
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    with pytest.raises(AccountValidationError) as error:
        await validate_account(session, acc.id)
    assert error.value.code == "missing_2fa_data"
    assert error.value.stage == "input"

    reloaded = await session.get(Account, acc.id)
    assert reloaded.status == "validation_failed"


@pytest.mark.asyncio
async def test_validate_account_invalid_totp_secret(session):
    """Invalid TOTP secret is reported without exposing it."""
    from app.services.account_validation import AccountValidationError, validate_account

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="badsecret@e.com",
        password_encrypted="pass123",
        totp_secret_encrypted="not-base32-@@@",
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    with pytest.raises(AccountValidationError) as error:
        await validate_account(session, acc.id)
    assert error.value.code == "invalid_totp"
    assert "not-base32" not in error.value.to_json()

    reloaded = await session.get(Account, acc.id)
    assert reloaded.status == "validation_failed"


@pytest.mark.asyncio
async def test_validate_account_enable_2fa_failure(session, monkeypatch):
    """The concrete 2FA UI stage is preserved in the job-facing exception."""
    from app.integrations.playwright.enable_2fa import Enable2FAError
    from app.services.account_validation import AccountValidationError, validate_account

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="enablefail@e.com",
        password_encrypted="pass123",
        totp_secret_encrypted="",
        email="enablefail@gmail.com",
        email_password_encrypted="mailpass",
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    @asynccontextmanager
    async def fake_browser_context(*args, **kw):
        yield object()

    async def failing_enable_2fa(context, login, password, email_provider, **kw):
        raise Enable2FAError(
            "QR-код не найден.",
            code="setup_2fa_qr_not_found",
            stage="setup_2fa_qr",
        )

    monkeypatch.setattr("app.services.account_validation.browser_context", fake_browser_context)
    monkeypatch.setattr("app.services.account_validation.enable_2fa", failing_enable_2fa)
    provider = MagicMock()
    provider.preflight = AsyncMock()
    monkeypatch.setattr(
        "app.services.account_validation.detect_imap_provider",
        lambda *_args, **_kwargs: provider,
    )

    with pytest.raises(AccountValidationError) as error:
        await validate_account(session, acc.id)
    assert error.value.code == "setup_2fa_qr_not_found"
    assert error.value.stage == "setup_2fa_qr"

    reloaded = await session.get(Account, acc.id)
    assert reloaded.status == "validation_failed"


async def test_outlook_validation_prefers_connected_graph_credential(
    session,
    monkeypatch,
):
    from app.config import get_settings
    from app.integrations.email.microsoft_graph_provider import (
        MicrosoftGraphEmailProvider,
    )
    from app.services.account_validation import _build_email_provider

    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_ID", "graph-client")
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_SECRET", "graph-secret")
    get_settings.cache_clear()
    account = Account(
        login="openai-owner@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        email="owner@outlook.com",
        email_password_encrypted="legacy-mail-password",
    )
    session.add(account)
    await session.flush()
    credential = EmailOAuthCredential(
        account_id=account.id,
        provider="microsoft_graph",
        email="OWNER@outlook.com",
        refresh_token_encrypted="graph-refresh",
        scopes="Mail.Read",
        status="connected",
    )
    session.add(credential)
    await session.commit()

    provider = await _build_email_provider(
        session,
        account,
        account.email,
        account.email_password_encrypted,
    )

    assert isinstance(provider, MicrosoftGraphEmailProvider)
    await provider._on_refresh_token("rotated-graph-refresh")
    await session.refresh(credential)
    assert credential.refresh_token_encrypted == "rotated-graph-refresh"
