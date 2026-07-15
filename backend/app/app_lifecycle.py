from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select

from app.check_job_queue import CheckJobQueue
from app.config import get_settings
from app.db.session import async_session_factory
from app.integrations.funpay.runner import FunPayRunner, RunnerCallbacks
from app.integrations.funpay.types import SaleStatus
from app.models.account import Account, AccountLimits
from app.models.audit import AuditLog
from app.models.lot import Lot
from app.models.rental import OCCUPYING_RENTAL_STATUSES, Order, Rental
from app.models.settings import SellerSettings
from app.scheduler import ScheduledTask, Scheduler
from app.services.bump import BumpService
from app.services.funpay_lifecycle import build_callbacks
from app.services.lot_auto_manager import LotAutoManager, ProvenanceMarkerSyncError
from app.services.lot_sync import LotSyncService
from app.services.offer_configuration import validate_offer_configurations
from app.services.order_processor import OrderProcessor
from app.services.order_notifications import (
    GlobalOrderNotificationDeliveryError,
    OrderNotificationService,
    buyer_confirmation_due,
    buyer_confirmation_missing,
)
from app.services.order_provenance import (
    exact_lot_binding_exists,
    is_verified_bot_sale_order,
    verified_sale_for_order_exists,
)
from app.services.sale_registry import SaleRegistryService, SalesSyncResult
from app.services.rental_service import (
    CREDENTIAL_DELIVERY_LEASE,
    CREDENTIAL_DELIVERY_MAX_ATTEMPTS,
    RentalService,
)
from app.services.delivery_policy import CREDENTIAL_DELIVERY_POLL_SECONDS
from app.services.rental_expiry import RentalExpiryService

logger = logging.getLogger(__name__)

_ORDER_RETRY_BASE = timedelta(minutes=1)
_ORDER_RETRY_MAX = timedelta(hours=1)


def _defer_order_retry(
    order: Order,
    error: str,
    *,
    now: datetime | None = None,
) -> None:
    """Schedule a fair retry without letting old failures starve new orders."""

    now = now or datetime.now(timezone.utc)
    order.fulfillment_attempts += 1
    # Saturate before exponentiation. Persisted attempts are attacker/DB state
    # and must never overflow Python or abort the whole scheduler batch.
    exponent = min(6, max(0, order.fulfillment_attempts - 1))
    delay_seconds = min(
        _ORDER_RETRY_BASE.total_seconds() * (2**exponent),
        _ORDER_RETRY_MAX.total_seconds(),
    )
    order.fulfillment_next_attempt_at = now + timedelta(seconds=delay_seconds)
    order.fulfillment_last_error = error[:128]


def _clear_order_retry(order: Order) -> None:
    order.fulfillment_attempts = 0
    order.fulfillment_next_attempt_at = None
    order.fulfillment_last_error = None


class FunPayUnavailableError(RuntimeError):
    """The requested live operation needs a connected FunPay transport."""


class AppLifecycle:
    """Own the FunPay transport and all periodic application tasks."""

    def __init__(self, golden_key: str, category_id: int) -> None:
        self._browser_concurrency_cap = get_settings().browser_concurrency_cap
        self._golden_key = golden_key
        self._category_id = category_id
        self.scheduler = Scheduler()
        self.runner: FunPayRunner | None = None
        self._gateway = None
        self.last_funpay_error: str | None = None
        self._funpay_lock = asyncio.Lock()
        self._capacity_reconcile_dirty = False
        self._capacity_reconcile_task: asyncio.Task[None] | None = None
        self._capacity_reconcile_stopping = False
        self._capacity_reconcile_retry_seconds = 5.0
        self._limits_interval_seconds = 5 * 60
        self._validation_interval_seconds = 24 * 60 * 60
        self._lot_interval_seconds = 10 * 60
        self._bump_interval_seconds = 4 * 60 * 60
        self._refresh_interval_seconds = 60
        self._pending_order_interval_seconds = CREDENTIAL_DELIVERY_POLL_SECONDS
        self._sale_sync_interval_seconds = 120
        self._refresh_concurrency = self._browser_concurrency_cap
        self._revoke_concurrency = self._browser_concurrency_cap
        self._revoke_semaphore = asyncio.Semaphore(self._revoke_concurrency)
        self._refresh_max_attempts = 3
        self._refresh_retry_delay_seconds = 5 * 60
        self._expiry = RentalExpiryService(
            capacity_changed=self.request_capacity_reconcile,
        )
        self._refunds = OrderProcessor()
        self._order_notifications = OrderNotificationService()
        self._rentals = RentalService(
            capacity_changed=self.request_capacity_reconcile,
        )
        self._bump = BumpService()
        self._jobs = CheckJobQueue()
        self._lot_sync = LotSyncService()
        self._sale_registry = SaleRegistryService()
        self._legacy_sales_bootstrapped = False

    async def start(self) -> None:
        """Start the live FunPay listener when a session key is configured."""
        self._capacity_reconcile_stopping = False
        await self.reconfigure_funpay()
        await self._recover_interrupted_validation_jobs()
        self._register_tasks()
        await self.scheduler.start()

    async def reconfigure_funpay(self, golden_key: str | None = None) -> bool:
        """Atomically replace the live FunPay transport without scheduler restart.

        ``None`` reloads the effective key from DB/environment.  An explicit
        empty string disables FunPay and stops the current listener.
        """
        async with self._funpay_lock:
            configured_key, node_id = await self._load_runtime_settings()
            effective_key = configured_key if golden_key is None else golden_key.strip()

            if not effective_key:
                old_runner = self.runner
                if old_runner is not None:
                    try:
                        await old_runner.stop()
                    except Exception as exc:
                        logger.exception("Old FunPay runner failed to stop")
                        self.last_funpay_error = str(exc)
                        await self._set_session_valid(
                            bool(getattr(old_runner, "started", False))
                        )
                        raise RuntimeError(
                            "FunPay runner could not be stopped"
                        ) from exc
                self.runner = None
                self._gateway = None
                self._golden_key = ""
                self.last_funpay_error = None
                await self._set_session_valid(False)
                return False

            old_runner = self.runner
            old_gateway = self._gateway
            old_key = self._golden_key
            old_error = self.last_funpay_error
            candidate: FunPayRunner | None = None
            try:
                candidate = FunPayRunner(
                    effective_key, RunnerCallbacks(), self._category_id,
                )
                gateway = candidate.gateway
                candidate.set_callbacks(build_callbacks(
                    async_session_factory,
                    gateway,
                    capacity_changed=self.request_capacity_reconcile,
                ))
                # Authenticate without enabling callbacks, then make the
                # ownership marker a startup barrier. Otherwise a sale of a
                # pre-migration lot can arrive before its immutable marker is
                # present on the order page and be lost as "unmanaged".
                prepare = getattr(candidate, "prepare", None)
                if prepare is not None:
                    await prepare()
                await self._sync_lot_markers_before_listener(gateway, node_id)
                # Fully start the candidate before touching the working
                # transport. A bad key must not disconnect the old bot.
                await candidate.start()
                if node_id:
                    try:
                        resolved = await gateway.get_category_id(node_id)
                    except Exception:
                        logger.exception("Unable to resolve FunPay parent category")
                    else:
                        if resolved:
                            self._category_id = resolved
                            candidate.category_id = resolved
                if old_runner is not None:
                    try:
                        await old_runner.stop()
                    except Exception:
                        logger.exception("Old FunPay runner failed to stop after swap")
                self.runner = candidate
                self._gateway = gateway
                self._golden_key = effective_key
                self.last_funpay_error = None
                await self._set_session_valid(True)
                if self._capacity_reconcile_dirty:
                    self.request_capacity_reconcile()
                return True
            except Exception as exc:
                logger.exception("FunPay runtime failed to start")
                if candidate is not None:
                    try:
                        await candidate.stop()
                    except Exception:
                        logger.exception("Failed to stop rejected FunPay candidate")
                self.runner = old_runner
                self._gateway = old_gateway
                self._golden_key = old_key
                self.last_funpay_error = (
                    old_error
                    if old_runner is not None
                    else str(exc)
                )
                await self._set_session_valid(
                    bool(old_runner is not None and getattr(old_runner, "started", False))
                )
                return False

    async def _sync_lot_markers_before_listener(
        self,
        gateway,
        node_id: int | None,
    ) -> None:
        """Retry a short, fail-closed marker pass before event intake."""

        last_error: ProvenanceMarkerSyncError | None = None
        for attempt in range(3):
            try:
                async with async_session_factory() as session:
                    manager = LotAutoManager(funpay_node_id=node_id or 0)
                    await manager.sync_missing_provenance_markers(
                        session,
                        gateway,
                        strict=True,
                    )
                return
            except ProvenanceMarkerSyncError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        if last_error is not None:
            raise last_error

    async def stop(self) -> None:
        """Остановка Scheduler (и Runner если есть)."""
        await self.scheduler.stop()
        self._capacity_reconcile_stopping = True
        capacity_task = self._capacity_reconcile_task
        if capacity_task is not None and not capacity_task.done():
            capacity_task.cancel()
            await asyncio.gather(capacity_task, return_exceptions=True)
        self._capacity_reconcile_task = None
        async with self._funpay_lock:
            if self.runner is not None:
                try:
                    await self.runner.stop()
                except Exception:
                    logger.exception("Runner stop failed")
                finally:
                    self.runner = None
                    self._gateway = None

    def request_capacity_reconcile(self) -> None:
        """Coalesce capacity changes into a non-blocking remote lot refresh.

        Callers invoke this only after committing their durable allocation or
        release.  The actual FunPay operation runs in its own task, so code
        that is already inside ``_funpay_lock`` can never self-deadlock.
        """

        if self._capacity_reconcile_stopping:
            return
        self._capacity_reconcile_dirty = True
        # Preserve the dirty state while disconnected. Reconfiguration starts
        # the worker as soon as a live transport is installed.
        if self._gateway is None:
            return
        task = self._capacity_reconcile_task
        if task is None or task.done():
            self._capacity_reconcile_task = asyncio.create_task(
                self._capacity_reconcile_loop(),
                name="capacity-lot-reconcile",
            )

    async def _capacity_reconcile_loop(self) -> None:
        """Drain the dirty flag; retain it across transient remote failures."""

        current_task = asyncio.current_task()
        retry_delay = self._capacity_reconcile_retry_seconds
        try:
            while (
                self._capacity_reconcile_dirty
                and not self._capacity_reconcile_stopping
            ):
                if self._gateway is None:
                    return
                self._capacity_reconcile_dirty = False
                try:
                    await self.reconcile_lots(refresh_stock=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The DB state is authoritative and already committed.
                    # Keep the dirty bit so this loop retries; the regular
                    # lot-auto task is an additional eventual-recovery path.
                    self._capacity_reconcile_dirty = True
                    logger.exception(
                        "Capacity lot reconciliation failed; retry scheduled"
                    )
                    if self._gateway is None:
                        return
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(
                        max(self._capacity_reconcile_retry_seconds, retry_delay * 2),
                        5 * 60,
                    )
                else:
                    retry_delay = self._capacity_reconcile_retry_seconds
                    # Let same-tick requests merge into at most one follow-up.
                    await asyncio.sleep(0)
        finally:
            if self._capacity_reconcile_task is current_task:
                self._capacity_reconcile_task = None
            # Close the narrow race where a caller marks dirty after the loop
            # condition but before task cleanup.
            if (
                self._capacity_reconcile_dirty
                and not self._capacity_reconcile_stopping
            ):
                self.request_capacity_reconcile()

    async def _load_runtime_settings(self) -> tuple[str, int | None]:
        """Prefer persisted settings while retaining environment fallbacks."""
        golden_key = self._golden_key.strip()
        node_id: int | None = None
        try:
            async with async_session_factory() as session:
                settings = await session.get(SellerSettings, 1)
                if settings is not None:
                    if settings.funpay_session_key and settings.funpay_session_key.strip():
                        golden_key = settings.funpay_session_key.strip()
                    node_id = settings.funpay_node_id
                    self._limits_interval_seconds = max(
                        60, settings.limits_check_interval_minutes * 60,
                    )
                    self._validation_interval_seconds = max(
                        60, settings.check_interval_minutes * 60,
                    )
                    self._bump_interval_seconds = max(
                        60, settings.bump_interval_hours * 60 * 60,
                    )
                    self._refresh_interval_seconds = max(
                        30, settings.check_delay_seconds,
                    )
                    self._refresh_concurrency = min(
                        self._browser_concurrency_cap,
                        max(1, settings.refresh_recover_concurrency),
                    )
                    self._refresh_max_attempts = max(
                        1, settings.refresh_max_attempts,
                    )
                    self._refresh_retry_delay_seconds = max(
                        60, settings.refresh_retry_delay_minutes * 60,
                    )
        except Exception:
            # Unit tests may construct the lifecycle without initializing a DB;
            # production initializes/migrates it before AppLifecycle.start().
            logger.exception("Failed to read FunPay runtime settings; using environment")
        return golden_key, node_id

    async def reload_settings(self) -> None:
        """Apply persisted scheduler settings without restarting the app."""
        await self._load_runtime_settings()
        self._register_tasks()

    async def _set_session_valid(self, valid: bool) -> None:
        try:
            async with async_session_factory() as session:
                settings = await session.get(SellerSettings, 1)
                if settings is not None:
                    settings.funpay_session_valid = valid
                    await session.commit()
        except Exception:
            logger.exception("Failed to persist FunPay session state")

    def _register_tasks(self) -> None:
        self.scheduler.register("expire_overdue", ScheduledTask(
            callback=self._task_expire_overdue, interval=30,
        ))
        self.scheduler.register("limits_check", ScheduledTask(
            callback=self._task_limits_check,
            interval=self._limits_interval_seconds,
        ))
        self.scheduler.register("scheduled_validation", ScheduledTask(
            callback=self._task_enqueue_scheduled_validations,
            interval=self._validation_interval_seconds,
        ))
        self.scheduler.register("lot_auto_manager", ScheduledTask(
            callback=self._task_lot_auto,
            interval=self._lot_interval_seconds,
        ))
        self.scheduler.register("bump", ScheduledTask(
            callback=self._task_bump,
            interval=self._bump_interval_seconds,
        ))
        self.scheduler.register("refresh_recover", ScheduledTask(
            callback=self._task_refresh_recover,
            interval=self._refresh_interval_seconds,
        ))
        self.scheduler.register("refund_revoke", ScheduledTask(
            callback=self._task_refund_revoke,
            interval=60,
        ))
        self.scheduler.register("pending_order_retry", ScheduledTask(
            callback=self._task_pending_orders,
            interval=self._pending_order_interval_seconds,
        ))
        self.scheduler.register("funpay_sale_sync", ScheduledTask(
            callback=self._task_funpay_sale_sync,
            interval=self._sale_sync_interval_seconds,
        ))
        self.scheduler.register("order_confirmed_notify", ScheduledTask(
            callback=self._task_order_confirmed_notify,
            interval=60,
        ))

    async def sync_funpay_sales(
        self,
        *,
        order_id: str | None = None,
    ) -> SalesSyncResult:
        """Run a sale-only sync, optionally for one exact FunPay order."""

        gateway = self._gateway
        if gateway is None:
            raise FunPayUnavailableError("FunPay bot is not connected")
        async with async_session_factory() as session:
            bootstrapped = 0
            if not self._legacy_sales_bootstrapped:
                bootstrapped = await self._sale_registry.bootstrap_from_orders(
                    session
                )
            result = (
                await self._sale_registry.sync_order(session, gateway, order_id)
                if order_id is not None
                else await self._sale_registry.sync_recent_sales(session, gateway)
            )
            profile_result = None
            if order_id is None:
                profile_result = await self._sale_registry.refresh_buyer_profiles(
                    session,
                    gateway,
                )
            await session.commit()
            self._legacy_sales_bootstrapped = True
        total_imported = bootstrapped + result.imported
        profiles_refreshed = (
            profile_result.refreshed if profile_result is not None else 0
        )
        profile_errors = profile_result.errors if profile_result is not None else 0
        logger.info(
            "FunPay sale sync%s: imported=%s enriched=%s profiles=%s errors=%s",
            f" order={order_id}" if order_id else "",
            total_imported,
            result.enriched,
            profiles_refreshed,
            result.enrichment_errors + result.history_errors + profile_errors,
        )
        return SalesSyncResult(
            imported=total_imported,
            enriched=result.enriched,
            enrichment_errors=result.enrichment_errors,
            history_errors=result.history_errors,
            profiles_refreshed=profiles_refreshed,
            profile_errors=profile_errors,
        )

    async def _task_funpay_sale_sync(self) -> None:
        if self._gateway is None:
            return
        await self.sync_funpay_sales()

    async def _task_order_confirmed_notify(self) -> None:
        """Retry due confirmations without poison-row head-of-line blocking."""

        gateway = self._gateway
        if gateway is None:
            return
        now = datetime.now(timezone.utc)
        async with async_session_factory() as session:
            filters = (
                exact_lot_binding_exists(Order),
                verified_sale_for_order_exists(Order),
                buyer_confirmation_due(Order),
                buyer_confirmation_missing(Order),
                Order.status == "completed",
                Order.confirmation_delivery_status.in_(("pending", "failed")),
                or_(
                    Order.confirmation_delivery_next_attempt_at.is_(None),
                    Order.confirmation_delivery_next_attempt_at <= now,
                ),
            )
            oldest = list((await session.execute(
                select(Order.id)
                .where(*filters)
                .order_by(
                    Order.created_at.asc(),
                    Order.id.asc(),
                )
                .limit(25)
            )).scalars())
            newest = list((await session.execute(
                select(Order.id)
                .where(*filters)
                .order_by(Order.created_at.desc(), Order.id.desc())
                .limit(25)
            )).scalars())
            oldest_ids = set(oldest)
            order_ids = oldest + [
                order_id for order_id in newest if order_id not in oldest_ids
            ]
            await session.commit()
        for order_id in order_ids:
            try:
                async with async_session_factory() as session:
                    await self._order_notifications.notify_confirmed(
                        session, gateway, order_id,
                    )
            except GlobalOrderNotificationDeliveryError:
                logger.warning(
                    "Stopped confirmed-order notification batch after shared "
                    "FunPay delivery failure on %s",
                    order_id,
                    exc_info=True,
                )
                break
            except Exception:
                logger.exception(
                    "Confirmed-order notification retry failed for %s",
                    order_id,
                )

    async def _task_expire_overdue(self) -> None:
        """Помечать истёкшие аренды как expired."""
        gateway = self._gateway
        async with async_session_factory() as session:
            candidates = await self._expiry.prepare_overdue_batch(session)
            await session.commit()

        async def expire_one(candidate: tuple[int, int]) -> None:
            rental_id, order_id = candidate
            async with self._revoke_semaphore:
                try:
                    # AsyncSession is stateful and not concurrency-safe. Every
                    # claim/revoke/finalize pipeline owns a dedicated session.
                    async with async_session_factory() as session:
                        await self._expiry.expire_candidate(
                            session,
                            gateway,
                            rental_id=rental_id,
                            order_id=order_id,
                        )
                except Exception:
                    logger.exception(
                        "Rental %s expiry revoke failed", rental_id,
                    )

        await asyncio.gather(*(expire_one(item) for item in candidates))
        if candidates:
            self.request_capacity_reconcile()

        if gateway is None:
            return
        async with async_session_factory() as session:
            notification_candidates = (
                await self._expiry.pending_notification_candidates(session)
            )
            await session.commit()

        notification_semaphore = asyncio.Semaphore(self._revoke_concurrency)

        async def notify_one(candidate: tuple[int, int]) -> None:
            rental_id, order_id = candidate
            async with notification_semaphore:
                try:
                    async with async_session_factory() as session:
                        await self._expiry.notify_expiration_candidate(
                            session,
                            gateway,
                            rental_id=rental_id,
                            order_id=order_id,
                        )
                except Exception:
                    logger.exception(
                        "Rental %s expiry notification failed", rental_id,
                    )

        await asyncio.gather(
            *(notify_one(item) for item in notification_candidates)
        )

    async def _task_limits_check(self) -> None:
        """Замер лимитов для аккаунтов с устаревшим measured_at."""
        async with async_session_factory() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=self._limits_interval_seconds,
            )
            reserved_targets = select(
                Rental.replacement_target_account_id
            ).where(Rental.replacement_target_account_id.is_not(None))
            occupied_accounts = select(Rental.account_id).where(
                Rental.status.in_(OCCUPYING_RENTAL_STATUSES)
            )
            result = await session.execute(
                select(AccountLimits)
                .join(Account, Account.id == AccountLimits.account_id)
                .where(
                    AccountLimits.refresh_status == "ok",
                    AccountLimits.account_id.not_in(reserved_targets),
                    AccountLimits.account_id.not_in(occupied_accounts),
                    or_(
                        AccountLimits.measured_at.is_(None),
                        AccountLimits.measured_at < cutoff,
                    ),
                )
                .with_for_update(of=Account, skip_locked=True)
            )
            for limits in result.scalars().all():
                await self._jobs.enqueue(
                    session,
                    account_id=limits.account_id,
                    priority="limit_check",
                    job_type="limit_check",
                )
            await session.commit()

    async def _task_enqueue_scheduled_validations(self) -> None:
        """Enqueue a real full validation for active accounts when due."""
        async with async_session_factory() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=self._validation_interval_seconds,
            )
            reserved_targets = select(
                Rental.replacement_target_account_id
            ).where(Rental.replacement_target_account_id.is_not(None))
            occupied_accounts = select(Rental.account_id).where(
                Rental.status.in_(OCCUPYING_RENTAL_STATUSES)
            )
            result = await session.execute(
                select(Account.id).where(
                    Account.status == "active",
                    Account.operator_status_override.is_(None),
                    Account.id.not_in(reserved_targets),
                    Account.id.not_in(occupied_accounts),
                    or_(
                        Account.chatgpt_last_check_at.is_(None),
                        Account.chatgpt_last_check_at <= cutoff,
                    ),
                )
                .with_for_update(of=Account, skip_locked=True)
            )
            due_account_ids = result.scalars().all()
            for account_id in due_account_ids:
                await self._jobs.enqueue(
                    session,
                    account_id=account_id,
                    priority="scheduled",
                    job_type="full_validation",
                )
            await session.commit()
        if due_account_ids:
            self.request_capacity_reconcile()

    async def _task_lot_auto(self) -> None:
        """Пересчёт capacity и sync лотов."""
        if self._gateway is None:
            return
        try:
            await self.reconcile_lots()
        except FunPayUnavailableError:
            # Reconfiguration can disconnect the transport between the cheap
            # guard above and lock acquisition.
            return

    async def reconcile_lots(
        self,
        *,
        refresh_stock: bool = False,
        refresh_published: bool = False,
    ) -> list:
        """Immediately reconcile local price/capacity state with FunPay."""
        async with self._funpay_lock:
            gateway = self._require_gateway()
            async with async_session_factory() as session:
                settings = await session.get(SellerSettings, 1)
                node_id = settings.funpay_node_id if settings else None
                # Catalog safety reconciliation must run even when the global
                # node is not configured: a manual lot can carry its own node.
                # LotAutoManager treats node 0 as "do not create new lots".
                mgr = LotAutoManager(funpay_node_id=node_id or 0)
                actions = await mgr.run(
                    session,
                    gateway,
                    refresh_stock=refresh_stock,
                    refresh_published=refresh_published,
                )
                await session.commit()
                return actions

    async def sync_manual_lot(self, lot_id: int, active: bool = True) -> int:
        """Create/update a local lot on the currently connected FunPay account."""
        async with self._funpay_lock:
            gateway = self._require_gateway()
            async with async_session_factory() as session:
                lot = await session.get(Lot, lot_id)
                if active and lot is not None:
                    await validate_offer_configurations(session, [lot])
                offer_id = await self._lot_sync.sync_lot(
                    session, gateway, lot_id, active,
                )
                lot = await session.get(Lot, lot_id)
                if lot is not None:
                    lot.status = "active" if active else "paused"
                    lot.paused_reason = None if active else "manual"
                await session.commit()
                return offer_id

    async def set_lot_active(self, lot_id: int, active: bool) -> None:
        """Toggle an already-published lot on the current FunPay account."""
        async with self._funpay_lock:
            gateway = self._require_gateway()
            async with async_session_factory() as session:
                if active:
                    lot = await session.get(Lot, lot_id)
                    if lot is not None:
                        await validate_offer_configurations(session, [lot])
                    await self._lot_sync.activate_lot(session, gateway, lot_id)
                    lot = await session.get(Lot, lot_id)
                    if lot is not None:
                        lot.paused_reason = None
                else:
                    await self._lot_sync.pause_lot(session, gateway, lot_id)
                    lot = await session.get(Lot, lot_id)
                    if lot is not None:
                        lot.paused_reason = "manual"
                await session.commit()

    async def delete_lot(self, lot_id: int) -> None:
        """Permanently delete a published bot-owned offer from FunPay."""

        async with self._funpay_lock:
            gateway = self._require_gateway()
            async with async_session_factory() as session:
                await self._lot_sync.delete_lot(
                    session,
                    gateway,
                    lot_id,
                )
                await session.commit()

    def _require_gateway(self):
        gateway = self._gateway
        if gateway is None:
            raise FunPayUnavailableError("FunPay is not connected")
        return gateway

    async def _task_bump(self) -> None:
        """Поднять лоты с истёкшим кулдауном bump."""
        if self._gateway is None:
            return
        async with async_session_factory() as session:
            settings = await session.get(SellerSettings, 1)
            if not settings or not settings.auto_bump_enabled:
                return
            interval = timedelta(hours=settings.bump_interval_hours)
            result = await session.execute(
                select(Lot).where(Lot.status == "active", Lot.funpay_id.isnot(None))
            )
            for lot in result.scalars().all():
                if await self._bump.needs_bump(session, lot.id, interval):
                    await self._bump.bump_lot(
                        session, self._gateway,
                        lot_id=lot.id,
                        category_id=self._category_id,
                        subcategory_id=settings.funpay_node_id or 0,
                    )
            await session.commit()

    async def _task_refresh_recover(self) -> None:
        """Enqueue due recoveries and process the validation queue in parallel."""
        from app.refresh_worker import RefreshRecoveryWorker

        await self._recover_stale_validation_leases()
        await self._enqueue_due_refresh_recoveries()

        async def process_one() -> bool:
            async with async_session_factory() as session:
                worker = RefreshRecoveryWorker(
                    max_attempts=self._refresh_max_attempts,
                )
                return await worker.process_next(session)

        processed = await asyncio.gather(
            *(process_one() for _ in range(self._refresh_concurrency))
        )
        # Validation/limit jobs can add or remove sellable accounts. Signal
        # once after the whole worker batch instead of reconciling per row.
        if any(processed):
            self.request_capacity_reconcile()

    async def _recover_interrupted_validation_jobs(self) -> None:
        """At startup, every worker lease belongs to the previous process."""
        try:
            async with async_session_factory() as session:
                await self._jobs.recover_stale_running(
                    session,
                    ("full_validation", "refresh_recover", "limit_check"),
                    stale_before=datetime.now(timezone.utc),
                )
                interrupted_device_accounts = await self._jobs.fail_active_jobs(
                    session,
                    ("device_auth",),
                    error=json.dumps(
                        {
                            "stage": "device_auth",
                            "code": "device_auth_server_restarted",
                            "detail": (
                                "Подтверждение входа прервано перезапуском сервера."
                            ),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                for account_id in interrupted_device_accounts:
                    account = await session.get(Account, account_id)
                    if account is not None:
                        if account.validation_rerun_requested:
                            account.validation_rerun_requested = False
                            account.status = (
                                account.operator_status_override
                                or "pending_validation"
                            )
                            await self._jobs.enqueue(
                                session,
                                account.id,
                                priority="manual",
                                job_type="full_validation",
                            )
                        else:
                            account.status = (
                                account.operator_status_override
                                or "validation_failed"
                            )
                await session.commit()
        except Exception:
            logger.exception("Failed to recover interrupted validation jobs")

    async def _recover_stale_validation_leases(self) -> None:
        async with async_session_factory() as session:
            await self._jobs.recover_stale_running(
                session,
                ("full_validation", "refresh_recover", "limit_check"),
            )
            await session.commit()

    async def _enqueue_due_refresh_recoveries(self) -> None:
        async with async_session_factory() as session:
            retry_before = datetime.now(timezone.utc) - timedelta(
                seconds=self._refresh_retry_delay_seconds,
            )
            result = await session.execute(
                select(AccountLimits.account_id).where(
                    AccountLimits.refresh_status == "expired",
                    AccountLimits.refresh_recover_attempts
                    < self._refresh_max_attempts,
                    or_(
                        AccountLimits.refresh_last_recover_at.is_(None),
                        AccountLimits.refresh_last_recover_at <= retry_before,
                    ),
                )
            )
            for account_id in result.scalars().all():
                await self._jobs.enqueue(
                    session,
                    account_id=account_id,
                    priority="refresh_recover",
                    job_type="refresh_recover",
                )
            await session.commit()

    async def _task_pending_orders(self) -> None:
        """Retry due paid orders fairly, with bounded delivery backoff."""
        async with self._funpay_lock:
            gateway = self._gateway
            if gateway is None:
                return
            async with async_session_factory() as session:
                settings = await session.get(SellerSettings, 1)
                max_rentals = (
                    settings.default_max_active_rentals if settings else 1
                )
                now = datetime.now(timezone.utc)
                stale_delivery_before = now - CREDENTIAL_DELIVERY_LEASE
                retry_rank = func.coalesce(
                    Rental.credentials_delivery_next_attempt_at,
                    Order.fulfillment_next_attempt_at,
                    Order.created_at,
                )
                result = await session.execute(
                    select(Order.id)
                    .outerjoin(Rental, Rental.order_id == Order.id)
                    .where(
                        exact_lot_binding_exists(Order),
                        verified_sale_for_order_exists(Order),
                        Order.status.in_(["pending", "completed"]),
                        or_(
                            and_(
                                Rental.id.is_(None),
                                or_(
                                    Order.fulfillment_next_attempt_at.is_(None),
                                    Order.fulfillment_next_attempt_at <= now,
                                ),
                            ),
                            and_(
                                Rental.status == "active",
                                or_(
                                    and_(
                                        Rental.credentials_delivery_status == "failed",
                                        Rental.credentials_delivery_attempts
                                        < CREDENTIAL_DELIVERY_MAX_ATTEMPTS,
                                        or_(
                                            Rental.credentials_delivery_next_attempt_at.is_(None),
                                            Rental.credentials_delivery_next_attempt_at <= now,
                                        ),
                                    ),
                                    and_(
                                        Rental.credentials_delivery_status == "sending",
                                        Rental.credentials_delivery_started_at.is_not(None),
                                        Rental.credentials_delivery_started_at
                                        <= stale_delivery_before,
                                    ),
                                ),
                            ),
                        ),
                    )
                    .order_by(
                        retry_rank.asc(),
                        Order.id.asc(),
                    )
                    .limit(50)
                )
                for order_id in result.scalars().all():
                    try:
                        order = await session.get(Order, order_id)
                        if order is None or order.status not in {
                            "pending", "completed",
                        }:
                            continue
                        remote = await gateway.get_order(order.funpay_order_id)
                        if remote.status is SaleStatus.REFUNDED:
                            await self._refunds.process_sale_refunded(
                                session, order.funpay_order_id,
                            )
                            await session.commit()
                            self.request_capacity_reconcile()
                            continue
                        if remote.status not in {
                            SaleStatus.PAID,
                            SaleStatus.COMPLETED,
                        }:
                            logger.warning(
                                "Pending order %s has non-fulfillable remote status %s",
                                order.funpay_order_id,
                                remote.status.value,
                            )
                            _defer_order_retry(
                                order,
                                f"remote_status:{remote.status.value}",
                            )
                            await session.commit()
                            continue
                        rental = await self._rentals.fulfill_order(
                            session,
                            gateway,
                            order_id,
                            max_rentals,
                            notify_unavailable=False,
                        )
                        if (
                            rental is not None
                            and rental.credentials_delivery_status == "sent"
                        ):
                            _clear_order_retry(order)
                            if remote.status is SaleStatus.COMPLETED:
                                order.status = "completed"
                                await self._order_notifications.mark_confirmed_due(
                                    session,
                                    order,
                                )
                        elif rental is None:
                            _defer_order_retry(order, "no_account_available")
                        elif rental.credentials_delivery_status == "manual":
                            order.fulfillment_next_attempt_at = None
                            order.fulfillment_last_error = (
                                rental.credentials_delivery_last_error
                                or "credential_delivery_manual_required"
                            )[:128]
                        await session.commit()
                    except Exception as exc:
                        await session.rollback()
                        logger.exception("Pending order %s retry failed", order_id)
                        retry_order = await session.get(Order, order_id)
                        if (
                            retry_order is not None
                            and retry_order.status in {"pending", "completed"}
                        ):
                            _defer_order_retry(
                                retry_order,
                                f"retry_failed:{type(exc).__name__}",
                            )
                            await session.commit()

    async def _task_refund_revoke(self) -> None:
        """Retry refunds whose external account revoke previously failed."""
        async with async_session_factory() as session:
            result = await session.execute(
                select(Order.funpay_order_id).where(
                    exact_lot_binding_exists(Order),
                    verified_sale_for_order_exists(Order),
                    Order.status == "refund_pending",
                )
            )
            order_ids = list(result.scalars().all())
            await session.commit()

        async def revoke_one(order_id: str) -> None:
            async with self._revoke_semaphore:
                try:
                    # OrderProcessor commits its durable claim before Kick I/O
                    # and finalizes it in this same independently-owned
                    # session. A slow account cannot hold the rest of the
                    # refund queue behind it.
                    async with async_session_factory() as session:
                        await self._refunds.process_sale_refunded(
                            session, order_id,
                        )
                except Exception:
                    logger.exception(
                        "Refund %s revoke retry failed", order_id,
                    )

        await asyncio.gather(*(revoke_one(order_id) for order_id in order_ids))
        if order_ids:
            self.request_capacity_reconcile()

    async def retry_rental_delivery(self, rental_id: int) -> None:
        """Explicit operator retry after resolving a manual delivery failure."""

        async with self._funpay_lock:
            gateway = self._gateway
            if gateway is None:
                raise FunPayUnavailableError("FunPay is not connected")
            async with async_session_factory() as session:
                # Snapshot only: never hold a row lock across remote FunPay
                # I/O. The authoritative mutation below uses the global
                # Order -> Rental lock order shared with refund/delivery.
                rental = (
                    await session.execute(
                        select(Rental)
                        .where(Rental.id == rental_id)
                    )
                ).scalar_one_or_none()
                if rental is None:
                    raise KeyError(f"Rental {rental_id} not found")
                if rental.status != "active":
                    raise ValueError("Only an active rental can be delivered")
                if rental.credentials_delivery_status == "sent":
                    return
                if rental.credentials_delivery_status == "sending":
                    started_at = rental.credentials_delivery_started_at
                    if started_at is not None and started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    if (
                        started_at is not None
                        and started_at
                        > datetime.now(timezone.utc) - CREDENTIAL_DELIVERY_LEASE
                    ):
                        raise ValueError("Credential delivery is already running")

                order = await session.get(Order, rental.order_id)
                if order is None or order.status not in {"pending", "completed"}:
                    raise ValueError("Order is no longer fulfillable")
                if not await is_verified_bot_sale_order(session, order):
                    raise ValueError("Order has no exact verified bot-sale provenance")
                remote = await gateway.get_order(order.funpay_order_id)
                if remote.status is SaleStatus.REFUNDED:
                    await self._refunds.process_sale_refunded(
                        session, order.funpay_order_id,
                    )
                    await session.commit()
                    raise ValueError("Order has been refunded")
                if remote.status not in {SaleStatus.PAID, SaleStatus.COMPLETED}:
                    raise ValueError(
                        f"Order is not fulfillable on FunPay ({remote.status.value})"
                    )

                order = (
                    await session.execute(
                        select(Order)
                        .where(Order.id == order.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if not await is_verified_bot_sale_order(session, order):
                    raise ValueError("Order has no exact verified bot-sale provenance")
                rental = (
                    await session.execute(
                        select(Rental)
                        .where(Rental.id == rental_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if order.status not in {"pending", "completed"}:
                    raise ValueError("Order is no longer fulfillable")
                if rental.order_id != order.id or rental.status != "active":
                    raise ValueError("Only an active rental can be delivered")
                if rental.credentials_delivery_status == "sent":
                    return
                if rental.credentials_delivery_status == "sending":
                    started_at = rental.credentials_delivery_started_at
                    if started_at is not None and started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    if (
                        started_at is not None
                        and started_at
                        > datetime.now(timezone.utc) - CREDENTIAL_DELIVERY_LEASE
                    ):
                        raise ValueError("Credential delivery is already running")

                previous_attempts = rental.credentials_delivery_attempts
                previous_error = rental.credentials_delivery_last_error
                rental.credentials_delivery_status = "failed"
                rental.credentials_delivery_attempts = (
                    min(
                        previous_attempts,
                        CREDENTIAL_DELIVERY_MAX_ATTEMPTS - 1,
                    )
                    if previous_attempts > 0
                    else 0
                )
                rental.credentials_delivery_started_at = None
                rental.credentials_delivery_next_attempt_at = None
                rental.credentials_delivery_last_error = None
                _clear_order_retry(order)
                session.add(
                    AuditLog(
                        event_type="credential_delivery_manual_retry",
                        account_id=rental.account_id,
                        rental_id=rental.id,
                        chat_id=rental.buyer_funpay_chat_id,
                        metadata_={
                            "previous_attempts": previous_attempts,
                            "previous_error": previous_error,
                        },
                    )
                )
                await session.commit()

                settings = await session.get(SellerSettings, 1)
                max_rentals = (
                    settings.default_max_active_rentals if settings else 1
                )
                await self._rentals.fulfill_order(
                    session,
                    gateway,
                    order.id,
                    max_rentals,
                    notify_unavailable=False,
                )
