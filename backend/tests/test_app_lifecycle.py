import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone
from app.app_lifecycle import AppLifecycle


def test_lifecycle_creates_components():
    lc = AppLifecycle(golden_key="", category_id=0)
    assert lc.scheduler is not None
    assert lc.runner is None  # без golden_key


async def test_lifecycle_start_stop_without_golden_key():
    """Без golden_key Runner не стартует, но Scheduler должен работать."""
    lc = AppLifecycle(golden_key="", category_id=0)
    await lc.start()
    assert lc.scheduler.running is True
    await lc.stop()
    assert lc.scheduler.running is False


async def test_register_periodic_tasks():
    lc = AppLifecycle(golden_key="", category_id=0)
    await lc.start()
    assert "expire_overdue" in lc.scheduler._tasks
    assert "limits_check" in lc.scheduler._tasks
    assert "lot_auto_manager" in lc.scheduler._tasks
    assert "bump" in lc.scheduler._tasks
    assert "refresh_recover" in lc.scheduler._tasks
    assert "refund_revoke" in lc.scheduler._tasks
    await lc.stop()


async def test_stop_is_idempotent():
    lc = AppLifecycle(golden_key="", category_id=0)
    await lc.stop()  # без start — не падает
    await lc.stop()


async def test_start_builds_live_runner_from_runtime_settings(monkeypatch):
    import app.app_lifecycle as lifecycle_module

    class Gateway:
        async def get_category_id(self, subcategory_id):
            assert subcategory_id == 55
            return 7

    class Runner:
        def __init__(self, key, callbacks, category_id):
            self.key = key
            self.callbacks = callbacks
            self.category_id = category_id
            self.gateway = Gateway()
            self.started = False

        def set_callbacks(self, callbacks):
            self.callbacks = callbacks

        async def start(self):
            self.started = True

        async def stop(self):
            self.started = False

    monkeypatch.setattr(lifecycle_module, "FunPayRunner", Runner)
    lc = AppLifecycle(golden_key="", category_id=0)
    monkeypatch.setattr(lc, "_load_runtime_settings", AsyncMock(return_value=("db-key", 55)))
    valid = AsyncMock()
    monkeypatch.setattr(lc, "_set_session_valid", valid)
    lc.scheduler.start = AsyncMock()
    lc.scheduler.stop = AsyncMock()

    await lc.start()

    assert lc.runner is not None and lc.runner.started is True
    assert lc.runner.key == "db-key"
    assert lc._gateway is lc.runner.gateway
    assert lc._category_id == 7
    valid.assert_awaited_once_with(True)
    await lc.stop()


def test_register_tasks_uses_loaded_intervals():
    lc = AppLifecycle(golden_key="", category_id=0)
    lc._limits_interval_seconds = 120
    lc._lot_interval_seconds = 180
    lc._bump_interval_seconds = 7200
    lc._refresh_interval_seconds = 45

    lc._register_tasks()

    assert lc.scheduler._tasks["limits_check"].interval == 120
    assert lc.scheduler._tasks["lot_auto_manager"].interval == 180
    assert lc.scheduler._tasks["bump"].interval == 7200
    assert lc.scheduler._tasks["refresh_recover"].interval == 45


async def test_load_runtime_settings_configures_scheduler(session, monkeypatch):
    import app.app_lifecycle as lifecycle_module
    from app.models.settings import SellerSettings

    session.add(SellerSettings(
        id=1,
        funpay_session_key="db-key",
        funpay_node_id=55,
        limits_check_interval_minutes=2,
        check_interval_minutes=3,
        bump_interval_hours=2,
        check_delay_seconds=45,
    ))
    await session.flush()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lc = AppLifecycle(golden_key="env-key", category_id=0)

    key, node = await lc._load_runtime_settings()

    assert (key, node) == ("db-key", 55)
    assert lc._limits_interval_seconds == 120
    assert lc._lot_interval_seconds == 180
    assert lc._bump_interval_seconds == 7200
    assert lc._refresh_interval_seconds == 45


async def test_limits_task_includes_never_measured_accounts(session, monkeypatch):
    import app.app_lifecycle as lifecycle_module
    from app.models.account import Account, AccountLimits
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    account = Account(
        login="unmeasured@example.com",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(account)
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="refresh",
        measured_at=None,
        refresh_status="ok",
    ))
    await session.flush()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    measure = AsyncMock()
    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    monkeypatch.setattr(lifecycle_module, "measure_account_limits", measure)

    await AppLifecycle("", 0)._task_limits_check()

    measure.assert_awaited_once_with(session, account.id)


async def test_reconfigure_funpay_swaps_runner_and_clear_stops_it(monkeypatch):
    import app.app_lifecycle as lifecycle_module

    class Gateway:
        async def get_category_id(self, _node_id):
            return 7

    class Runner:
        instances = []

        def __init__(self, key, callbacks, category_id):
            self.key = key
            self.callbacks = callbacks
            self.category_id = category_id
            self.gateway = Gateway()
            self.started = False
            self.stopped = False
            self.instances.append(self)

        def set_callbacks(self, callbacks):
            self.callbacks = callbacks

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True
            self.started = False

    monkeypatch.setattr(lifecycle_module, "FunPayRunner", Runner)
    lifecycle = AppLifecycle("old", 0)
    old = Runner("old", None, 0)
    old.started = True
    lifecycle.runner = old
    lifecycle._gateway = old.gateway
    monkeypatch.setattr(
        lifecycle,
        "_load_runtime_settings",
        AsyncMock(return_value=("db-key", 55)),
    )
    valid = AsyncMock()
    monkeypatch.setattr(lifecycle, "_set_session_valid", valid)

    assert await lifecycle.reconfigure_funpay("new-key") is True
    current = lifecycle.runner
    assert old.stopped is True
    assert current is not None and current.key == "new-key" and current.started
    assert lifecycle._gateway is current.gateway

    assert await lifecycle.reconfigure_funpay("") is False
    assert current.stopped is True
    assert lifecycle.runner is None
    assert lifecycle._gateway is None
    assert [call.args[0] for call in valid.await_args_list] == [True, False]
