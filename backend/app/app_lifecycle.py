from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.account import AccountLimits
from app.models.lot import Lot
from app.models.settings import SellerSettings
from app.scheduler import ScheduledTask, Scheduler
from app.services.account_limits import measure_account_limits
from app.services.bump import BumpService
from app.services.lot_auto_manager import LotAutoManager
from app.services.rental_expiry import RentalExpiryService

logger = logging.getLogger(__name__)


class AppLifecycle:
    """Оркестратор приложения: Scheduler + периодические задачи.

    FunPayRunner создаётся только если golden_key задан (Task 6 интегрирует).
    Periodic tasks используют async_session_factory напрямую.
    """

    def __init__(self, golden_key: str, category_id: int) -> None:
        self._golden_key = golden_key
        self._category_id = category_id
        self.scheduler = Scheduler()
        self.runner = None  # FunPayRunner, если golden_key задан
        self._gateway = None  # ChatGateway, если golden_key
        self._expiry = RentalExpiryService()
        self._bump = BumpService()

    async def start(self) -> None:
        """Регистрация задач + старт Scheduler."""
        self._register_tasks()
        await self.scheduler.start()

    async def stop(self) -> None:
        """Остановка Scheduler (и Runner если есть)."""
        if self.runner is not None:
            try:
                await self.runner.stop()
            except Exception:
                logger.exception("Runner stop failed")
        await self.scheduler.stop()

    def _register_tasks(self) -> None:
        self.scheduler.register("expire_overdue", ScheduledTask(
            callback=self._task_expire_overdue, interval=30,
        ))
        self.scheduler.register("limits_check", ScheduledTask(
            callback=self._task_limits_check, interval=300,
        ))
        self.scheduler.register("lot_auto_manager", ScheduledTask(
            callback=self._task_lot_auto, interval=120,
        ))
        self.scheduler.register("bump", ScheduledTask(
            callback=self._task_bump, interval=3600,
        ))
        self.scheduler.register("refresh_recover", ScheduledTask(
            callback=self._task_refresh_recover, interval=60,
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
                    AccountLimits.measured_at < cutoff,
                )
            )
            for limits in result.scalars().all():
                await measure_account_limits(session, limits.account_id)
            await session.commit()

    async def _task_lot_auto(self) -> None:
        """Пересчёт capacity и sync лотов."""
        if self._gateway is None:
            return
        async with async_session_factory() as session:
            settings = await session.get(SellerSettings, 1)
            node_id = settings.funpay_node_id if settings else None
            if node_id:
                mgr = LotAutoManager(funpay_node_id=node_id)
                await mgr.run(session, self._gateway)
                await session.commit()

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
