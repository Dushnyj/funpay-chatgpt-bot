import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone
from app.app_lifecycle import (
    AppLifecycle,
    FunPayUnavailableError,
    _defer_order_retry,
)
from app.models.rental import Order


def test_lifecycle_creates_components():
    lc = AppLifecycle(golden_key="", category_id=0)
    assert lc.scheduler is not None
    assert lc.runner is None  # без golden_key


def test_order_retry_backoff_saturates_for_corrupt_large_attempt_count():
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    order = Order(
        funpay_order_id="overflow-safe",
        funpay_chat_id="1",
        buyer_funpay_id="2",
        buyer_locale="ru",
        price=1,
        status="pending",
        fulfillment_attempts=1024,
    )

    _defer_order_retry(order, "x" * 200, now=now)

    assert order.fulfillment_attempts == 1025
    assert order.fulfillment_next_attempt_at == now + timedelta(hours=1)
    assert len(order.fulfillment_last_error) == 128


async def test_lifecycle_start_stop_without_golden_key():
    """Без golden_key Runner не стартует, но Scheduler должен работать."""
    lc = AppLifecycle(golden_key="", category_id=0)
    await lc.start()
    assert lc.scheduler.running is True
    await lc.stop()
    assert lc.scheduler.running is False


async def test_manual_delivery_retry_rechecks_remote_refund(monkeypatch):
    import app.app_lifecycle as lifecycle_module
    from app.integrations.funpay.types import OrderInfo, SaleStatus
    from app.models.rental import Rental

    order = Order(
        id=17,
        funpay_order_id="refunded-before-manual-retry",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        price=100,
        status="completed",
    )
    rental = Rental(
        id=23,
        order_id=order.id,
        account_id=31,
        buyer_funpay_id="200",
        buyer_funpay_chat_id="100",
        tier_id=1,
        duration_id=1,
        limit_scope_id=1,
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
        replacement_count=0,
        credentials_delivery_status="manual",
        credentials_delivery_template="welcome",
        credentials_delivery_attempts=5,
        credentials_delivery_last_error="prior failure",
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = rental
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=order)
    session.commit = AsyncMock()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._gateway = AsyncMock()
    lifecycle._gateway.get_order.return_value = OrderInfo(
        order_id=order.funpay_order_id,
        status=SaleStatus.REFUNDED,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="Offer",
        price=100,
    )
    lifecycle._refunds.process_sale_refunded = AsyncMock(return_value=order)
    lifecycle._rentals.fulfill_order = AsyncMock()

    with pytest.raises(ValueError, match="refunded"):
        await lifecycle.retry_rental_delivery(rental.id)

    lifecycle._refunds.process_sale_refunded.assert_awaited_once_with(
        session,
        order.funpay_order_id,
    )
    lifecycle._rentals.fulfill_order.assert_not_awaited()


async def test_manual_retry_preserves_nonzero_disclosure_attempts(monkeypatch):
    import app.app_lifecycle as lifecycle_module
    from app.integrations.funpay.types import OrderInfo, SaleStatus
    from app.models.rental import Rental

    order = Order(
        id=18,
        funpay_order_id="paid-manual-retry",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        price=100,
        status="completed",
    )
    rental = Rental(
        id=24,
        order_id=order.id,
        account_id=31,
        buyer_funpay_id="200",
        buyer_funpay_chat_id="100",
        tier_id=1,
        duration_id=1,
        limit_scope_id=1,
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        status="active",
        credentials_delivery_status="manual",
        credentials_delivery_template="welcome",
        credentials_delivery_attempts=4,
        credentials_delivery_last_error="ambiguous prior send",
    )

    def result(value):
        wrapped = MagicMock()
        wrapped.scalar_one_or_none.return_value = value
        wrapped.scalar_one.return_value = value
        return wrapped

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[result(rental), result(order), result(rental)]
    )

    async def get(model, _key):
        return order if model is Order else None

    session.get = AsyncMock(side_effect=get)
    session.commit = AsyncMock()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._gateway = AsyncMock()
    lifecycle._gateway.get_order.return_value = OrderInfo(
        order_id=order.funpay_order_id,
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="Offer",
        price=100,
    )
    lifecycle._rentals.fulfill_order = AsyncMock(return_value=rental)

    await lifecycle.retry_rental_delivery(rental.id)

    assert rental.credentials_delivery_attempts == 4
    lifecycle._rentals.fulfill_order.assert_awaited_once()


async def test_register_periodic_tasks():
    lc = AppLifecycle(golden_key="", category_id=0)
    await lc.start()
    assert "expire_overdue" in lc.scheduler._tasks
    assert "limits_check" in lc.scheduler._tasks
    assert "scheduled_validation" in lc.scheduler._tasks
    assert "lot_auto_manager" in lc.scheduler._tasks
    assert "bump" in lc.scheduler._tasks
    assert "refresh_recover" in lc.scheduler._tasks
    assert "refund_revoke" in lc.scheduler._tasks
    assert "pending_order_retry" in lc.scheduler._tasks
    assert "funpay_sale_sync" in lc.scheduler._tasks
    assert lc.scheduler._tasks["funpay_sale_sync"].interval == 120
    await lc.stop()


async def test_sale_sync_bootstraps_legacy_orders_only_once(monkeypatch):
    import app.app_lifecycle as lifecycle_module
    from app.services.sale_registry import ProfileRefreshResult, SalesSyncResult

    session = MagicMock()
    session.commit = AsyncMock()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._gateway = object()
    lifecycle._sale_registry.bootstrap_from_orders = AsyncMock(return_value=3)
    lifecycle._sale_registry.sync_recent_sales = AsyncMock(
        return_value=SalesSyncResult(
            imported=2,
            enriched=0,
            enrichment_errors=0,
        )
    )
    lifecycle._sale_registry.refresh_buyer_profiles = AsyncMock(
        return_value=ProfileRefreshResult(refreshed=1, errors=0)
    )

    first = await lifecycle.sync_funpay_sales()
    second = await lifecycle.sync_funpay_sales()

    assert first.imported == 5
    assert second.imported == 2
    assert first.profiles_refreshed == second.profiles_refreshed == 1
    lifecycle._sale_registry.bootstrap_from_orders.assert_awaited_once_with(session)
    assert lifecycle._sale_registry.sync_recent_sales.await_count == 2
    assert lifecycle._sale_registry.refresh_buyer_profiles.await_count == 2


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
    lc._validation_interval_seconds = 240
    lc._lot_interval_seconds = 180
    lc._bump_interval_seconds = 7200
    lc._refresh_interval_seconds = 45

    lc._register_tasks()

    assert lc.scheduler._tasks["limits_check"].interval == 120
    assert lc.scheduler._tasks["scheduled_validation"].interval == 240
    assert lc.scheduler._tasks["lot_auto_manager"].interval == 180
    assert lc.scheduler._tasks["bump"].interval == 7200
    assert lc.scheduler._tasks["refresh_recover"].interval == 45


def test_full_validation_fallback_interval_is_daily():
    lifecycle = AppLifecycle(golden_key="", category_id=0)

    assert lifecycle._validation_interval_seconds == 24 * 60 * 60


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
        refresh_recover_concurrency=4,
        refresh_max_attempts=5,
        refresh_retry_delay_minutes=7,
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
    assert lc._validation_interval_seconds == 180
    assert lc._lot_interval_seconds == 600
    assert lc._bump_interval_seconds == 7200
    assert lc._refresh_interval_seconds == 45
    assert lc._refresh_concurrency == 4
    assert lc._refresh_max_attempts == 5
    assert lc._refresh_retry_delay_seconds == 420


async def test_limits_task_includes_never_measured_accounts(session, monkeypatch):
    import app.app_lifecycle as lifecycle_module
    from sqlalchemy import select

    from app.models.account import Account, AccountCheckJob, AccountLimits
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

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())

    await AppLifecycle("", 0)._task_limits_check()

    job = (await session.execute(select(AccountCheckJob))).scalar_one()
    assert job.account_id == account.id
    assert job.priority == "limit_check"
    assert job.job_type == "limit_check"


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


async def test_reconfigure_funpay_rejects_bad_candidate_without_stopping_old(
    monkeypatch,
):
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
            if self.key == "bad-key":
                raise RuntimeError("rejected")
            self.started = True

        async def stop(self):
            self.stopped = True
            self.started = False

    monkeypatch.setattr(lifecycle_module, "FunPayRunner", Runner)
    lifecycle = AppLifecycle("old-key", 0)
    old = Runner("old-key", None, 0)
    old.started = True
    lifecycle.runner = old
    lifecycle._gateway = old.gateway
    lifecycle.last_funpay_error = None
    monkeypatch.setattr(
        lifecycle,
        "_load_runtime_settings",
        AsyncMock(return_value=("old-key", 55)),
    )
    valid = AsyncMock()
    monkeypatch.setattr(lifecycle, "_set_session_valid", valid)

    connected = await lifecycle.reconfigure_funpay("bad-key")

    candidate = Runner.instances[-1]
    assert connected is False
    assert lifecycle.runner is old
    assert lifecycle._gateway is old.gateway
    assert lifecycle._golden_key == "old-key"
    assert old.started is True
    assert old.stopped is False
    assert candidate.stopped is True
    valid.assert_awaited_once_with(True)


async def test_reconfigure_funpay_clear_surfaces_runner_stop_failure(monkeypatch):
    lifecycle = AppLifecycle("old-key", 0)
    old = MagicMock()
    old.started = True
    old.stop = AsyncMock(side_effect=RuntimeError("transport stuck"))
    old_gateway = object()
    lifecycle.runner = old
    lifecycle._gateway = old_gateway
    lifecycle._golden_key = "old-key"
    monkeypatch.setattr(
        lifecycle,
        "_load_runtime_settings",
        AsyncMock(return_value=("old-key", 55)),
    )
    valid = AsyncMock()
    monkeypatch.setattr(lifecycle, "_set_session_valid", valid)

    with pytest.raises(RuntimeError, match="could not be stopped"):
        await lifecycle.reconfigure_funpay("")

    assert lifecycle.runner is old
    assert lifecycle._gateway is old_gateway
    assert lifecycle._golden_key == "old-key"
    valid.assert_awaited_once_with(True)


async def test_scheduled_validation_enqueues_only_due_active_accounts(
    session, monkeypatch,
):
    import app.app_lifecycle as lifecycle_module
    from sqlalchemy import select

    from app.models.account import Account, AccountCheckJob
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="scheduled-tier", is_active=True)
    session.add(tier)
    await session.flush()
    now = datetime.now(timezone.utc)
    due = Account(
        login="due@example.com",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
        chatgpt_last_check_at=now - timedelta(hours=1),
    )
    fresh = Account(
        login="fresh@example.com",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
        chatgpt_last_check_at=now,
    )
    failed = Account(
        login="failed@example.com",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="validation_failed",
        chatgpt_last_check_at=now - timedelta(hours=1),
    )
    session.add_all([due, fresh, failed])
    await session.commit()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._validation_interval_seconds = 10 * 60

    await lifecycle._task_enqueue_scheduled_validations()

    jobs = (await session.execute(select(AccountCheckJob))).scalars().all()
    assert [(job.account_id, job.priority, job.job_type) for job in jobs] == [
        (due.id, "scheduled", "full_validation")
    ]


async def test_startup_recovers_worker_job_and_terminalizes_device_auth(
    session, monkeypatch,
):
    import app.app_lifecycle as lifecycle_module

    from app.models.account import Account, AccountCheckJob
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="restart-tier", is_active=True)
    session.add(tier)
    await session.flush()
    worker_account = Account(
        login="worker-restart@example.com",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="pending_validation",
    )
    device_account = Account(
        login="device-restart@example.com",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add_all([worker_account, device_account])
    await session.flush()
    worker_job = AccountCheckJob(
        account_id=worker_account.id,
        priority="scheduled",
        job_type="full_validation",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    device_job = AccountCheckJob(
        account_id=device_account.id,
        priority="manual",
        job_type="device_auth",
        status="pending",
    )
    session.add_all([worker_job, device_job])
    await session.commit()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())

    await AppLifecycle("", 0)._recover_interrupted_validation_jobs()

    await session.refresh(worker_job)
    await session.refresh(device_job)
    await session.refresh(device_account)
    assert worker_job.status == "pending"
    assert device_job.status == "failed"
    assert "device_auth_server_restarted" in device_job.error
    assert device_account.status == "validation_failed"


async def test_limits_scheduler_deduplicates_queued_measurement_job(
    session, monkeypatch,
):
    import app.app_lifecycle as lifecycle_module
    from sqlalchemy import select

    from app.models.account import Account, AccountCheckJob, AccountLimits
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="refresh-tier", is_active=True)
    session.add(tier)
    await session.flush()
    account = Account(
        login="expired@example.com",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
    )
    session.add(account)
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="refresh",
        measured_at=None,
        refresh_status="ok",
    ))
    await session.commit()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    await lifecycle._task_limits_check()
    await lifecycle._task_limits_check()

    jobs = (await session.execute(select(AccountCheckJob))).scalars().all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.account_id == account.id
    assert job.priority == "limit_check"
    assert job.job_type == "limit_check"
    assert job.status == "pending"


async def test_due_refresh_recovery_respects_delay_and_max_attempts(
    session, monkeypatch,
):
    import app.app_lifecycle as lifecycle_module
    from sqlalchemy import select

    from app.models.account import Account, AccountCheckJob, AccountLimits
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="retry-tier", is_active=True)
    session.add(tier)
    await session.flush()
    accounts = []
    for index in range(3):
        account = Account(
            login=f"retry-{index}@example.com",
            password_encrypted="pass",
            totp_secret_encrypted="JBSWY3DPEHPK3PXP",
            tier_id=tier.id,
            status="validation_failed",
        )
        session.add(account)
        await session.flush()
        accounts.append(account)
    now = datetime.now(timezone.utc)
    session.add_all([
        AccountLimits(
            account_id=accounts[0].id,
            refresh_token_encrypted="r0",
            refresh_status="expired",
            refresh_recover_attempts=1,
            refresh_last_recover_at=now - timedelta(minutes=10),
        ),
        AccountLimits(
            account_id=accounts[1].id,
            refresh_token_encrypted="r1",
            refresh_status="expired",
            refresh_recover_attempts=1,
            refresh_last_recover_at=now,
        ),
        AccountLimits(
            account_id=accounts[2].id,
            refresh_token_encrypted="r2",
            refresh_status="expired",
            refresh_recover_attempts=3,
            refresh_last_recover_at=now - timedelta(minutes=10),
        ),
    ])
    await session.commit()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._refresh_retry_delay_seconds = 5 * 60
    lifecycle._refresh_max_attempts = 3

    await lifecycle._enqueue_due_refresh_recoveries()

    jobs = (await session.execute(select(AccountCheckJob))).scalars().all()
    assert [job.account_id for job in jobs] == [accounts[0].id]


async def test_refresh_task_uses_configured_concurrency(monkeypatch):
    lifecycle = AppLifecycle("", 0)
    lifecycle._refresh_concurrency = 4
    lifecycle._refresh_max_attempts = 6
    lifecycle._recover_stale_validation_leases = AsyncMock()
    lifecycle._enqueue_due_refresh_recoveries = AsyncMock()

    process = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.refresh_worker.RefreshRecoveryWorker.process_next",
        process,
    )

    class Context:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "app.app_lifecycle.async_session_factory", lambda: Context()
    )

    await lifecycle._task_refresh_recover()

    assert process.await_count == 4


async def test_reload_settings_updates_validation_not_lot_interval(
    session, monkeypatch,
):
    import app.app_lifecycle as lifecycle_module
    from app.models.settings import SellerSettings

    settings = SellerSettings(
        id=1,
        check_interval_minutes=12,
        limits_check_interval_minutes=4,
    )
    session.add(settings)
    await session.commit()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._register_tasks()

    await lifecycle.reload_settings()

    assert lifecycle.scheduler._tasks["scheduled_validation"].interval == 720
    assert lifecycle.scheduler._tasks["limits_check"].interval == 240
    assert lifecycle.scheduler._tasks["lot_auto_manager"].interval == 600


async def test_pending_order_retry_suppresses_duplicate_unavailable_message(
    session, monkeypatch,
):
    import app.app_lifecycle as lifecycle_module
    from app.integrations.funpay.types import OrderInfo, SaleStatus
    from app.models.rental import Order

    order = Order(
        funpay_order_id="retry-order",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        price=100,
        status="pending",
    )
    session.add(order)
    await session.commit()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._gateway = AsyncMock()
    lifecycle._gateway.get_order.return_value = OrderInfo(
        order_id=order.funpay_order_id,
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title=None,
        price=100,
    )
    lifecycle._rentals.fulfill_order = AsyncMock(return_value=None)

    await lifecycle._task_pending_orders()

    lifecycle._rentals.fulfill_order.assert_awaited_once_with(
        session,
        lifecycle._gateway,
        order.id,
        1,
        notify_unavailable=False,
    )


async def test_pending_order_retry_does_not_fulfill_remote_refund(
    session, monkeypatch,
):
    import app.app_lifecycle as lifecycle_module
    from app.integrations.funpay.types import OrderInfo, SaleStatus
    from app.models.rental import Order

    order = Order(
        funpay_order_id="refunded-order",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        price=100,
        status="pending",
    )
    session.add(order)
    await session.commit()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    gateway = AsyncMock()
    gateway.get_order.return_value = OrderInfo(
        order_id=order.funpay_order_id,
        status=SaleStatus.REFUNDED,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title=None,
        price=100,
    )
    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._gateway = gateway
    lifecycle._rentals.fulfill_order = AsyncMock()

    await lifecycle._task_pending_orders()

    lifecycle._rentals.fulfill_order.assert_not_awaited()
    await session.refresh(order)
    assert order.status == "refunded"


async def test_public_lot_methods_require_connected_funpay():
    lifecycle = AppLifecycle("", 0)

    with pytest.raises(FunPayUnavailableError):
        await lifecycle.sync_manual_lot(1)
    with pytest.raises(FunPayUnavailableError):
        await lifecycle.set_lot_active(1, True)
    with pytest.raises(FunPayUnavailableError):
        await lifecycle.reconcile_lots()


async def test_sync_manual_lot_uses_runtime_gateway_and_separate_session(
    session, monkeypatch,
):
    import app.app_lifecycle as lifecycle_module
    from app.models.catalog import Duration, LimitScope, SubscriptionTier
    from app.models.lot import Lot

    tier = SubscriptionTier(
        code="plus",
        name="lot-tier",
        is_active=True,
        is_sellable=True,
    )
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=1)
    scope = LimitScope(code="any", name="Any", is_enabled=True)
    session.add_all([tier, duration, scope])
    await session.flush()
    lot = Lot(
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        title_ru="Лот",
        title_en="Lot",
        status="paused",
        auto_created=False,
    )
    session.add(lot)
    await session.commit()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._gateway = object()
    lifecycle._lot_sync.sync_lot = AsyncMock(return_value=321)

    offer_id = await lifecycle.sync_manual_lot(lot.id, active=True)

    assert offer_id == 321
    lifecycle._lot_sync.sync_lot.assert_awaited_once_with(
        session, lifecycle._gateway, lot.id, True
    )
    await session.refresh(lot)
    assert lot.status == "active"
    assert lot.paused_reason is None


async def test_reconcile_pauses_invalid_manual_lot_without_global_node(
    session,
    monkeypatch,
):
    import app.app_lifecycle as lifecycle_module
    from app.integrations.funpay.gateway import FakeChatGateway
    from app.models.catalog import Duration, LimitScope, SubscriptionTier
    from app.models.lot import Lot, PriceMatrix

    invalid_tier = SubscriptionTier(
        code="plus",
        name="invalid-lot-tier",
        is_active=True,
        is_sellable=False,
    )
    valid_tier = SubscriptionTier(
        code="pro_5x",
        name="valid-lot-tier",
        is_active=True,
        is_sellable=True,
    )
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=1)
    scope = LimitScope(code="any", name="Any", is_enabled=True)
    session.add_all([invalid_tier, valid_tier, duration, scope])
    await session.flush()
    invalid_manual_lot = Lot(
        funpay_node_id=55,
        tier_id=invalid_tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        title_ru="Лот",
        title_en="Lot",
        status="active",
        auto_created=False,
        funpay_id="901",
    )
    matrix = PriceMatrix(
        tier_id=valid_tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=799,
    )
    valid_auto_lot = Lot(
        config_key=matrix.config_key,
        funpay_node_id=55,
        tier_id=valid_tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Не менять",
        title_en="Do not change",
        description_ru="Сохранить",
        description_en="Keep",
        status="active",
        auto_created=True,
        funpay_id="902",
    )
    session.add_all([invalid_manual_lot, matrix, valid_auto_lot])
    await session.commit()

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(lifecycle_module, "async_session_factory", lambda: Context())
    lifecycle = AppLifecycle("", 0)
    lifecycle._gateway = FakeChatGateway()

    actions = await lifecycle.reconcile_lots()

    assert [(action.lot_id, action.action) for action in actions] == [
        (invalid_manual_lot.id, "pause")
    ]
    await session.refresh(invalid_manual_lot)
    assert invalid_manual_lot.status == "paused"
    assert invalid_manual_lot.paused_reason == "catalog_unavailable"

    await session.refresh(valid_auto_lot)
    assert valid_auto_lot.funpay_node_id == 55
    assert valid_auto_lot.status == "active"
    assert valid_auto_lot.price == 599
    assert valid_auto_lot.title_ru == "Не менять"
    assert valid_auto_lot.title_en == "Do not change"
    assert valid_auto_lot.description_ru == "Сохранить"
    assert valid_auto_lot.description_en == "Keep"


async def test_expiry_revoke_batch_does_not_head_of_line_block(monkeypatch):
    import asyncio
    import app.app_lifecycle as lifecycle_module

    sessions: list[object] = []

    class Session:
        async def commit(self):
            return None

    class Context:
        def __init__(self):
            self.session = Session()
            sessions.append(self.session)

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        lifecycle_module, "async_session_factory", lambda: Context(),
    )

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_finalized = asyncio.Event()
    worker_sessions: dict[int, object] = {}

    class Expiry:
        async def prepare_overdue_batch(self, _session):
            return [(1, 101), (2, 102)]

        async def expire_candidate(
            self, session, _gateway, *, rental_id, order_id,
        ):
            assert order_id == rental_id + 100
            worker_sessions[rental_id] = session
            if rental_id == 1:
                first_started.set()
                await release_first.wait()
            else:
                second_finalized.set()

    lifecycle = AppLifecycle("", 0)
    lifecycle._expiry = Expiry()

    task = asyncio.create_task(lifecycle._task_expire_overdue())
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.wait_for(second_finalized.wait(), timeout=1)

    assert not task.done()
    assert worker_sessions[1] is not worker_sessions[2]
    assert worker_sessions[1] is not sessions[0]

    release_first.set()
    await asyncio.wait_for(task, timeout=1)


async def test_refund_revoke_batch_does_not_head_of_line_block(monkeypatch):
    import asyncio
    import app.app_lifecycle as lifecycle_module

    sessions: list[object] = []

    class ScalarResult:
        class Scalars:
            @staticmethod
            def all():
                return ["slow-refund", "fast-refund"]

        @staticmethod
        def scalars():
            return ScalarResult.Scalars()

    class Session:
        async def execute(self, _statement):
            return ScalarResult()

        async def commit(self):
            return None

    class Context:
        def __init__(self):
            self.session = Session()
            sessions.append(self.session)

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        lifecycle_module, "async_session_factory", lambda: Context(),
    )

    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    fast_finalized = asyncio.Event()
    worker_sessions: dict[str, object] = {}

    async def process_refund(session, order_id):
        worker_sessions[order_id] = session
        if order_id == "slow-refund":
            slow_started.set()
            await release_slow.wait()
        else:
            fast_finalized.set()

    lifecycle = AppLifecycle("", 0)
    lifecycle._refunds.process_sale_refunded = AsyncMock(
        side_effect=process_refund,
    )

    task = asyncio.create_task(lifecycle._task_refund_revoke())
    await asyncio.wait_for(slow_started.wait(), timeout=1)
    await asyncio.wait_for(fast_finalized.wait(), timeout=1)

    assert not task.done()
    assert worker_sessions["slow-refund"] is not worker_sessions["fast-refund"]
    assert worker_sessions["slow-refund"] is not sessions[0]

    release_slow.set()
    await asyncio.wait_for(task, timeout=1)
