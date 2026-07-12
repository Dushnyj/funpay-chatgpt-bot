import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

# Импортируем модели и сервисы на уровне модуля, чтобы они зарегистрировались
# в Base.metadata до того, как фикстура test_engine создаст таблицы.
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier
from app.services.account_limits import MeasureResult, measure_account_limits


@pytest.mark.asyncio
async def test_measure_and_update_success(session, httpx_mock):
    """Полный цикл замера: refresh + usage + metadata → запись в AccountLimits."""

    # Подготовка: аккаунт с протухшим access_token (нужен refresh)
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="u@e.com",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
    )
    session.add(acc)
    await session.flush()

    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="valid-refresh-token",
        access_token_encrypted="old-expired-access",
        access_token_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),  # протух
        account_id_openai="acc-openai-1",
        refresh_status="ok",
    )
    session.add(limits)
    await session.commit()

    # Мок: refresh возвращает новые токены
    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        json={
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "id_token": _make_jwt({"email": "u@e.com", "https://api.openai.com/auth": {"plan_type": "plus"}}),
        },
    )
    # Мок: wham/usage
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/wham/usage",
        method="GET",
        json={
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 20, "reset_at": "2026-07-12T18:00:00Z"},
                "secondary_window": {"used_percent": 50, "reset_at": "2026-07-14T00:00:00Z"},
            },
        },
    )
    # Мок: accounts/check
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        method="GET",
        json={
            "accounts": {
                "acc-openai-1": {
                    "account": {"plan_type": "plus"},
                    "entitlement": {"expires_at": "2026-08-15T00:00:00Z"},
                }
            }
        },
    )

    result = await measure_account_limits(session, acc.id)
    assert result == MeasureResult.OK

    # Проверяем обновлённые поля
    reloaded = await session.get(AccountLimits, acc.id)
    assert reloaded.access_token_encrypted == "fresh-access"
    assert reloaded.refresh_token_encrypted == "fresh-refresh"
    assert reloaded.chat_5h_remaining_pct is None
    assert reloaded.codex_5h_remaining_pct == 80
    assert reloaded.chat_weekly_remaining_pct is None
    assert reloaded.codex_weekly_remaining_pct == 50
    assert reloaded.plan_type == "plus"
    assert reloaded.measured_at is not None
    assert reloaded.refresh_status == "ok"
    reloaded_account = await session.get(Account, acc.id)
    assert reloaded_account.tier_id == tier.id
    assert reloaded_account.plan_raw_type == "plus"
    assert reloaded_account.plan_source == "accounts_check+wham_usage"
    assert reloaded_account.plan_confidence == pytest.approx(0.92)
    assert reloaded_account.plan_detected_at is not None


@pytest.mark.asyncio
async def test_measure_refresh_failed_sets_status(session, httpx_mock):
    """Протухший refresh_token → RefreshFailedError → refresh_status=expired."""
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="bad@e.com",
        password_encrypted="pass",
        totp_secret_encrypted="totp",
        tier_id=tier.id,
        status="active",
    )
    session.add(acc)
    await session.flush()

    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="expired-refresh",
        access_token_encrypted="expired-access",
        access_token_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        refresh_status="ok",
    )
    session.add(limits)
    await session.commit()

    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        status_code=401,
        text="invalid_grant",
    )

    result = await measure_account_limits(session, acc.id)
    assert result == MeasureResult.REFRESH_FAILED

    reloaded = await session.get(AccountLimits, acc.id)
    assert reloaded.refresh_status == "expired"
    assert reloaded.refresh_failed_at is not None


@pytest.mark.asyncio
async def test_measure_skips_refresh_if_token_fresh(session, httpx_mock):
    """Свежий access_token — refresh не вызывается, только usage+metadata."""
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    known_expiry = datetime.now(timezone.utc) + timedelta(days=30)
    acc = Account(
        login="fresh@e.com",
        password_encrypted="p",
        totp_secret_encrypted="t",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=known_expiry,
    )
    session.add(acc)
    await session.flush()

    # access_token истекает через час — свежий
    future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="rt",
        access_token_encrypted="valid-access",
        access_token_expires_at=future,
        account_id_openai="acc-1",
        refresh_status="ok",
    )
    session.add(limits)
    await session.commit()

    # Только usage и metadata — refresh НЕ мокаем (если вызовется, тест упадёт)
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/wham/usage",
        method="GET",
        json={"plan_type": "plus", "rate_limit": None},
    )
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        method="GET",
        json={"accounts": {"acc-1": {"account": {"plan_type": "plus"}, "entitlement": {"expires_at": None}}}},
    )

    result = await measure_account_limits(session, acc.id)
    assert result == MeasureResult.OK

    reloaded = await session.get(AccountLimits, acc.id)
    # access_token не изменился
    assert reloaded.access_token_encrypted == "valid-access"
    assert acc.subscription_expires_at == known_expiry
    assert reloaded.subscription_expires_at == known_expiry


@pytest.mark.asyncio
async def test_measure_conflicting_plan_signals_clear_sellable_tier(session, httpx_mock):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    acc = Account(
        login="conflict@e.com",
        password_encrypted="p",
        totp_secret_encrypted="t",
        tier_id=tier.id,
        status="active",
    )
    session.add(acc)
    await session.flush()
    session.add(
        AccountLimits(
            account_id=acc.id,
            refresh_token_encrypted="rt",
            access_token_encrypted="valid-access",
            access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            account_id_openai="acc-conflict",
        )
    )
    await session.commit()

    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/wham/usage",
        method="GET",
        json={"plan_type": "plus", "rate_limit": None},
    )
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        method="GET",
        json={
            "accounts": {
                "acc-conflict": {
                    "account": {"plan_type": "pro"},
                    "entitlement": {"expires_at": None},
                }
            }
        },
    )

    assert await measure_account_limits(session, acc.id) == MeasureResult.OK
    await session.refresh(acc)
    limits = await session.get(AccountLimits, acc.id)
    assert acc.tier_id is None
    assert acc.plan_raw_type == "pro | plus"
    assert acc.plan_confidence == 0.0
    assert limits.plan_type == "unknown"


@pytest.mark.asyncio
async def test_measure_uses_id_token_plan_as_conservative_fallback(session, httpx_mock):
    account = Account(
        login="fallback@e.com",
        password_encrypted="p",
        totp_secret_encrypted="t",
        tier_id=None,
        status="pending_validation",
    )
    session.add(account)
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="rt",
        access_token_encrypted="valid-access",
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        account_id_openai="workspace",
    ))
    await session.commit()
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/wham/usage",
        method="GET",
        json={"plan_type": None, "rate_limit": None},
    )
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        method="GET",
        json={"accounts": {}},
    )

    assert await measure_account_limits(
        session,
        account.id,
        claim_plan_type="go",
    ) == MeasureResult.OK
    await session.refresh(account)
    assert account.plan_raw_type == "go"
    assert account.plan_source == "id_token"
    assert account.tier_id is not None
    assert (await session.get(SubscriptionTier, account.tier_id)).code == "go"


# Вспомогательная для JWT
def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."
