import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

# Импортируем модели и сервисы на уровне модуля, чтобы они зарегистрировались
# в Base.metadata до того, как фикстура test_engine создаст таблицы.
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier
from app.integrations.openai.types import UsageInfo
from app.services.account_limits import (
    MeasureResult,
    _acquire_access_token,
    _remaining_for_window,
    measure_account_limits,
)


@pytest.mark.parametrize(
    ("usage", "window_seconds", "expected"),
    [
        (
            UsageInfo(
                primary_remaining_pct=73,
                primary_window_seconds=7 * 24 * 60 * 60,
            ),
            7 * 24 * 60 * 60,
            73,
        ),
        (
            UsageInfo(
                primary_remaining_pct=70,
                primary_window_seconds=7 * 24 * 60 * 60,
                secondary_remaining_pct=60,
                secondary_window_seconds=5 * 60 * 60,
            ),
            5 * 60 * 60,
            60,
        ),
        (
            UsageInfo(
                primary_remaining_pct=70,
                primary_window_seconds=7 * 24 * 60 * 60,
                secondary_remaining_pct=60,
                secondary_window_seconds=5 * 60 * 60,
            ),
            7 * 24 * 60 * 60,
            70,
        ),
    ],
)
def test_legacy_usage_aliases_are_resolved_by_duration_not_position(
    usage: UsageInfo,
    window_seconds: int,
    expected: int,
):
    assert _remaining_for_window(usage, window_seconds) == expected


@pytest.mark.asyncio
async def test_stale_401_reuses_token_rotated_by_previous_worker(session):
    account = Account(
        login="refresh-race@example.com",
        password_encrypted="password",
        totp_secret_encrypted="totp",
        status="active",
    )
    session.add(account)
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="already-rotated-refresh",
        access_token_encrypted="already-rotated-access",
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        refresh_status="ok",
    ))
    await session.commit()

    with patch(
        "app.services.account_limits.refresh_access_token",
        new=AsyncMock(),
    ) as refresh:
        _limits, access_token = await _acquire_access_token(
            session,
            account.id,
            force=True,
            stale_access_token="token-that-received-401",
        )

    assert access_token == "already-rotated-access"
    refresh.assert_not_awaited()
    assert not session.in_transaction()


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
                "primary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 18000,
                    "reset_at": "2026-07-12T18:00:00Z",
                },
                "secondary_window": {
                    "used_percent": 50,
                    "limit_window_seconds": 604800,
                    "reset_at": "2026-07-14T00:00:00Z",
                },
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
                    "entitlement": {
                        "has_active_subscription": True,
                        "expires_at": "2026-08-15T00:00:00Z",
                    },
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
    assert reloaded.codex_5h_remaining_pct == 80
    assert reloaded.codex_weekly_remaining_pct == 50
    assert reloaded.codex_primary_remaining_pct == 80
    assert reloaded.codex_primary_window_seconds == 18000
    assert reloaded.codex_primary_resets_at == datetime(
        2026, 7, 12, 18, tzinfo=timezone.utc
    )
    assert reloaded.codex_secondary_remaining_pct == 50
    assert reloaded.codex_secondary_window_seconds == 604800
    assert reloaded.codex_secondary_resets_at == datetime(
        2026, 7, 14, tzinfo=timezone.utc
    )
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
async def test_measure_current_free_claims_preserves_sellable_override_and_exact_window(
    session, httpx_mock
):
    free_tier = SubscriptionTier(
        code="free",
        name="Free",
        description="ChatGPT Free",
        is_active=True,
        system_managed=True,
        # Explicit operator override: measuring an account must not re-enable
        # sale of this plan.
        is_sellable=False,
        sort_order=10,
    )
    session.add(free_tier)
    await session.flush()
    stale_expiry = datetime(2025, 1, 1, tzinfo=timezone.utc)
    account = Account(
        login="current@e.com",
        password_encrypted="p",
        totp_secret_encrypted="t",
        status="active",
        subscription_expires_at=stale_expiry,
    )
    session.add(account)
    await session.flush()
    access_token = _make_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-current",
            "chatgpt_plan_type": "free",
        },
        "https://api.openai.com/profile": {"email": "current@e.com"},
    })
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="rt",
        access_token_encrypted=access_token,
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        account_id_openai=None,
        subscription_expires_at=stale_expiry,
    ))
    await session.commit()
    reset_at = 1783987200
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/wham/usage",
        method="GET",
        json={
            "plan_type": "free",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 7,
                    "limit_window_seconds": 2592000,
                    "reset_at": reset_at,
                }
            },
        },
    )
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        method="GET",
        json={
            "accounts": {
                "acct-current": {
                    "account": {"plan_type": "free"},
                    "entitlement": {
                        "subscription_plan": "chatgptplusplan",
                        "has_active_subscription": False,
                        "expires_at": 1700000000,
                    },
                }
            }
        },
    )

    assert await measure_account_limits(session, account.id) == MeasureResult.OK

    limits = await session.get(AccountLimits, account.id)
    await session.refresh(account)
    assert limits.account_id_openai == "acct-current"
    assert limits.plan_type == "free"
    assert account.tier_id == free_tier.id
    assert account.plan_raw_type == "free"
    assert account.plan_source == "accounts_check+wham_usage+access_token"
    assert account.plan_confidence == pytest.approx(0.89)
    assert free_tier.is_sellable is False
    assert account.subscription_expires_at is None
    assert limits.subscription_expires_at is None
    assert limits.codex_primary_remaining_pct == 93
    assert limits.codex_primary_window_seconds == 2592000
    observed_reset = limits.codex_primary_resets_at
    if observed_reset.tzinfo is None:
        observed_reset = observed_reset.replace(tzinfo=timezone.utc)
    assert observed_reset == datetime.fromtimestamp(reset_at, tz=timezone.utc)
    assert limits.codex_secondary_remaining_pct is None
    assert limits.codex_secondary_window_seconds is None
    assert limits.codex_secondary_resets_at is None
    assert limits.codex_5h_remaining_pct is None
    assert limits.codex_weekly_remaining_pct is None
    requests = httpx_mock.get_requests()
    assert all(
        request.headers["chatgpt-account-id"] == "acct-current"
        for request in requests
    )


@pytest.mark.asyncio
async def test_measure_survives_accounts_check_cloudflare_403(
    session, httpx_mock
):
    free_tier = SubscriptionTier(
        code="free",
        name="Free",
        is_active=True,
        system_managed=True,
        is_sellable=False,
    )
    session.add(free_tier)
    await session.flush()
    account = Account(
        login="cloudflare@e.com",
        password_encrypted="p",
        totp_secret_encrypted="t",
        status="pending_validation",
    )
    session.add(account)
    await session.flush()
    access_token = _make_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-cloudflare",
            "chatgpt_plan_type": "free",
        }
    })
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="rt",
        access_token_encrypted=access_token,
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    ))
    await session.commit()

    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/wham/usage",
        method="GET",
        json={
            "plan_type": "free",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 5,
                    "limit_window_seconds": 2_592_000,
                    "reset_at": 1_786_493_497,
                }
            },
        },
    )
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        method="GET",
        status_code=403,
        text="<!doctype html><title>Just a moment</title>",
        headers={"content-type": "text/html"},
    )

    assert await measure_account_limits(session, account.id) == MeasureResult.OK
    await session.refresh(account)
    limits = await session.get(AccountLimits, account.id)
    assert account.tier_id == free_tier.id
    assert account.plan_raw_type == "free"
    assert account.plan_source == "wham_usage+access_token"
    assert account.subscription_expires_at is None
    assert limits.codex_primary_remaining_pct == 95
    assert limits.codex_primary_window_seconds == 2_592_000
    assert limits.refresh_status == "ok"


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
    assert reloaded.refresh_recover_attempts == 0


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
        json={
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 18000,
                },
                "secondary_window": {
                    "used_percent": 30,
                    "limit_window_seconds": 604800,
                },
            },
        },
    )
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        method="GET",
        json={"accounts": {"acc-1": {"account": {"plan_type": "plus"}, "entitlement": {"has_active_subscription": True, "expires_at": None}}}},
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

    assert (
        await measure_account_limits(session, acc.id)
        == MeasureResult.PLAN_DETECTION_FAILED
    )
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
        json={
            "plan_type": None,
            "rate_limit": {
                "primary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 18000,
                },
                "secondary_window": {
                    "used_percent": 30,
                    "limit_window_seconds": 604800,
                },
            },
        },
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
