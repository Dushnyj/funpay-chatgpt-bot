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
from app.integrations.openai.oauth import RefreshedTokens
from app.integrations.playwright.proxy import BrowserProxy, ProxyUnavailableError
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
async def test_refresh_token_uses_account_selected_proxy(session, monkeypatch):
    account = Account(
        login="routed-refresh@example.com",
        password_encrypted="password",
        totp_secret_encrypted="totp",
        status="active",
    )
    session.add(account)
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="refresh-token",
        access_token_encrypted="expired-access",
        access_token_expires_at=None,
        refresh_status="ok",
    ))
    await session.commit()
    selected = BrowserProxy(31, "socks5", "home-relay", 1080)
    resolved_for: list[int] = []
    refreshed_with: list[BrowserProxy | None] = []

    async def fake_resolve(_session, target):
        resolved_for.append(target.id)
        return selected

    async def fake_refresh(token, *, proxy=None):
        assert token == "refresh-token"
        refreshed_with.append(proxy)
        return RefreshedTokens("new-access", "new-refresh", None)

    monkeypatch.setattr(
        "app.services.account_limits.resolve_browser_proxy", fake_resolve
    )
    monkeypatch.setattr(
        "app.services.account_limits.refresh_access_token", fake_refresh
    )

    _limits, access_token = await _acquire_access_token(session, account.id)

    assert access_token == "new-access"
    assert resolved_for == [account.id]
    assert refreshed_with == [selected]


@pytest.mark.asyncio
async def test_refresh_proxy_failure_marks_selected_route_offline(
    session,
    monkeypatch,
):
    account = Account(
        login="offline-refresh@example.com",
        password_encrypted="password",
        totp_secret_encrypted="totp",
        status="active",
    )
    session.add(account)
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="refresh-token",
        access_token_encrypted="expired-access",
        access_token_expires_at=None,
        refresh_status="ok",
    ))
    await session.commit()
    selected = BrowserProxy(32, "socks5", "home-relay", 1080)
    marked: list[BrowserProxy | None] = []

    async def fake_resolve(_session, _target):
        return selected

    async def failing_refresh(_token, *, proxy=None):
        assert proxy is selected
        raise ProxyUnavailableError()

    async def fake_mark(_session, proxy, **_kwargs):
        marked.append(proxy)
        return True

    monkeypatch.setattr(
        "app.services.account_limits.resolve_browser_proxy", fake_resolve
    )
    monkeypatch.setattr(
        "app.services.account_limits.refresh_access_token", failing_refresh
    )
    monkeypatch.setattr(
        "app.services.account_limits.mark_proxy_route_offline", fake_mark
    )

    with pytest.raises(ProxyUnavailableError):
        await _acquire_access_token(session, account.id)

    assert marked == [selected]
    assert not session.in_transaction()


@pytest.mark.asyncio
async def test_measure_pins_one_route_for_refresh_usage_and_account_check(
    session,
    monkeypatch,
):
    tier = SubscriptionTier(
        code="free",
        name="Free",
        is_active=True,
        system_managed=True,
        is_sellable=False,
    )
    account = Account(
        login="pinned-measure@example.com",
        password_encrypted="password",
        totp_secret_encrypted="totp",
        status="active",
    )
    session.add_all([tier, account])
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="refresh-token",
        access_token_encrypted="expired-access",
        access_token_expires_at=None,
        account_id_openai="openai-account",
        refresh_status="ok",
    ))
    await session.commit()

    selected = BrowserProxy(
        73,
        "socks5",
        "home-relay.internal",
        1080,
        config_revision=11,
    )
    resolved: list[int] = []
    refreshed: list[BrowserProxy | None] = []
    clients: list[BrowserProxy | None] = []
    calls: list[str] = []

    async def fake_resolve(_session, target):
        resolved.append(target.id)
        return selected

    async def fake_refresh(_token, *, proxy=None):
        refreshed.append(proxy)
        return RefreshedTokens("fresh-access", "fresh-refresh", None)

    class FakeOpenAIClient:
        def __init__(self, _token, _account_id, *, proxy=None):
            clients.append(proxy)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get_usage(self):
            calls.append("usage")
            return UsageInfo(
                plan_type="free",
                primary_remaining_pct=95,
                primary_window_seconds=30 * 24 * 60 * 60,
            )

        async def get_account_metadata(self):
            calls.append("accounts_check")
            from app.integrations.openai.types import AccountMetadata

            return AccountMetadata(
                plan_type="free",
                has_active_subscription=False,
            )

    monkeypatch.setattr(
        "app.services.account_limits.resolve_browser_proxy", fake_resolve
    )
    monkeypatch.setattr(
        "app.services.account_limits.refresh_access_token", fake_refresh
    )
    monkeypatch.setattr(
        "app.services.account_limits.OpenAIClient", FakeOpenAIClient
    )

    result = await measure_account_limits(session, account.id)

    assert result is MeasureResult.OK
    assert resolved == [account.id]
    assert refreshed == [selected]
    assert clients == [selected]
    assert calls == ["usage", "accounts_check"]


@pytest.mark.asyncio
async def test_usage_transport_failure_marks_the_pinned_route_revision_offline(
    session,
    monkeypatch,
):
    account = Account(
        login="usage-route-failure@example.com",
        password_encrypted="password",
        totp_secret_encrypted="totp",
        status="active",
    )
    session.add(account)
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="refresh-token",
        access_token_encrypted="fresh-access",
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        refresh_status="ok",
    ))
    await session.commit()

    selected = BrowserProxy(
        74,
        "socks5",
        "home-relay.internal",
        1080,
        config_revision=13,
    )
    marked: list[BrowserProxy | None] = []

    async def fake_resolve(_session, _target):
        return selected

    async def fake_mark(_session, proxy, **_kwargs):
        marked.append(proxy)
        return True

    class FailingOpenAIClient:
        def __init__(self, _token, _account_id, *, proxy=None):
            assert proxy is selected

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get_usage(self):
            raise ProxyUnavailableError()

    monkeypatch.setattr(
        "app.services.account_limits.resolve_browser_proxy", fake_resolve
    )
    monkeypatch.setattr(
        "app.services.account_limits.mark_proxy_route_offline", fake_mark
    )
    monkeypatch.setattr(
        "app.services.account_limits.OpenAIClient", FailingOpenAIClient
    )

    with pytest.raises(ProxyUnavailableError):
        await measure_account_limits(session, account.id)

    assert marked == [selected]
    assert marked[0].config_revision == 13
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
    assert reloaded_account.subscription_expiry_source == "accounts_check"
    assert reloaded.subscription_expiry_source == "accounts_check"


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
    assert account.subscription_expiry_source is None
    assert limits.subscription_expiry_source is None
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
    # A pre-provenance/manual value is not promoted when accounts/check omits
    # the deadline.
    assert acc.subscription_expires_at is None
    assert acc.subscription_expiry_source is None
    assert reloaded.subscription_expires_at is None
    assert reloaded.subscription_expiry_source is None


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
