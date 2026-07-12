from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.db.session import async_session_factory
from app.integrations.funpay.runner import FunPayRunner, RunnerCallbacks
from app.models.account import AccountLimits
from app.models.lot import Lot
from app.models.rental import Order
from app.models.settings import SellerSettings
from app.scheduler import ScheduledTask, Scheduler
from app.services.account_limits import measure_account_limits
from app.services.bump import BumpService
from app.services.funpay_lifecycle import build_callbacks
from app.services.lot_auto_manager import LotAutoManager
from app.services.order_processor import OrderProcessor
from app.services.rental_expiry import RentalExpiryService

logger = logging.getLogger(__name__)


class AppLifecycle:
    """Own the FunPay transport and all periodic application tasks."""

    def __init__(self, golden_key: str, category_id: int) -> None:
        self._golden_key = golden_key
        self._category_id = category_id
        self.scheduler = Scheduler()
        self.runner: FunPayRunner | None = None
        self._gateway = None
        self.last_funpay_error: str | None = None
        self._funpay_lock = asyncio.Lock()
        self._limits_interval_seconds = 5 * 60
        self._lot_interval_seconds = 10 * 60
        self._bump_interval_seconds = 4 * 60 * 60
        self._refresh_interval_seconds = 60
        self._expiry = RentalExpiryService()
        self._refunds = OrderProcessor()
        self._bump = BumpService()

    async def start(self) -> None:
        """Start the live FunPay listener when a session key is configured."""
        await self.reconfigure_funpay()
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

            old_runner = self.runner
            self.runner = None
            self._gateway = None
            if old_runner is not None:
                try:
                    await old_runner.stop()
                except Exception:
                    logger.exception("Old FunPay runner failed to stop")

            if not effective_key:
                self._golden_key = ""
                self.last_funpay_error = None
                await self._set_session_valid(False)
                return False

            runner: FunPayRunner | None = None
            try:
                runner = FunPayRunner(
                    effective_key, RunnerCallbacks(), self._category_id,
                )
                gateway = runner.gateway
                runner.set_callbacks(build_callbacks(async_session_factory, gateway))
                await runner.start()
                if node_id:
                    try:
                        resolved = await gateway.get_category_id(node_id)
                    except Exception:
                        logger.exception("Unable to resolve FunPay parent category")
                    else:
                        if resolved:
                            self._category_id = resolved
                            runner.category_id = resolved
                self.runner = runner
                self._gateway = gateway
                self._golden_key = effective_key
                self.last_funpay_error = None
                await self._set_session_valid(True)
                return True
            except Exception as exc:
                self.last_funpay_error = str(exc)
                logger.exception("FunPay runtime failed to start")
                if runner is not None:
                    await runner.stop()
                self.runner = None
                self._gateway = None
                await self._set_session_valid(False)
                return False

    async def stop(self) -> None:
        """Остановка Scheduler (и Runner если есть)."""
        if self.runner is not None:
            try:
                await self.runner.stop()
            except Exception:
                logger.exception("Runner stop failed")
            finally:
                self.runner = None
                self._gateway = None
        await self.scheduler.stop()

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
                    self._lot_interval_seconds = max(
                        60, settings.check_interval_minutes * 60,
                    )
                    self._bump_interval_seconds = max(
                        60, settings.bump_interval_hours * 60 * 60,
                    )
                    self._refresh_interval_seconds = max(
                        30, settings.check_delay_seconds,
                    )
        except Exception:
            # Unit tests may construct the lifecycle without initializing a DB;
            # production initializes/migrates it before AppLifecycle.start().
            logger.exception("Failed to read FunPay runtime settings; using environment")
        return golden_key, node_id

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

    async def _task_expire_overdue(self) -> None:
        """Помечать истёкшие аренды как expired."""
        async with async_session_factory() as session:
            await self._expiry.expire_overdue(session, self._gateway)
            await session.commit()

    async def _task_limits_check(self) -> None:
        """Замер лимитов для аккаунтов с устаревшим measured_at."""
        async with async_session_factory() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
            result = await session.execute(
                select(AccountLimits).where(
                    AccountLimits.refresh_status == "ok",
                    or_(
                        AccountLimits.measured_at.is_(None),
                        AccountLimits.measured_at < cutoff,
                    ),
                )
            )
            for limits in result.scalars().all():
                await measure_account_limits(session, limits.account_id)
            await session.commit()

    async def _task_lot_auto(self) -> None:
        """Пересчёт capacity и sync лотов."""
        await self.reconcile_lots()

    async def reconcile_lots(self) -> list:
        """Immediately reconcile local price/capacity state with FunPay."""
        if self._gateway is None:
            return []
        async with async_session_factory() as session:
            settings = await session.get(SellerSettings, 1)
            node_id = settings.funpay_node_id if settings else None
            if node_id:
                mgr = LotAutoManager(funpay_node_id=node_id)
                actions = await mgr.run(session, self._gateway)
                await session.commit()
                return actions
        return []

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
        """Обработать один pending refresh-recovery job."""
        from app.refresh_worker import RefreshRecoveryWorker
        async with async_session_factory() as session:
            settings = await session.get(SellerSettings, 1)
            delay = settings.check_delay_seconds if settings else 45
            worker = RefreshRecoveryWorker(check_delay_seconds=delay)
            await worker.process_next(session)

    async def _task_refund_revoke(self) -> None:
        """Retry refunds whose external account revoke previously failed."""
        async with async_session_factory() as session:
            result = await session.execute(
                select(Order.funpay_order_id).where(Order.status == "refund_pending")
            )
            for order_id in result.scalars().all():
                await self._refunds.process_sale_refunded(session, order_id)
            await session.commit()
