from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.lot import BumpLog, Lot


@dataclass(frozen=True)
class BumpResult:
    """Результат операции bump лота."""

    success: bool
    error: str | None = None


class BumpService:
    """Поднятие лотов на FunPay (bump категории) с записью в BumpLog.

    raise_offers бампит всю подкатегорию, но мы логируем per-lot,
    так как один лот = одна нода. Равномерность: один bump за вызов.
    """

    async def bump_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot_id: int,
        category_id: int,
        subcategory_id: int,
    ) -> BumpResult:
        lot = await session.get(Lot, lot_id)
        if lot is None:
            raise KeyError(f"Lot {lot_id} not found")
        try:
            await gateway.bump_category(category_id, subcategory_id)
        except Exception as exc:
            error = str(exc)
            await self._log(session, lot.id, success=False, error=error)
            return BumpResult(success=False, error=error)
        await self._log(session, lot.id, success=True, error=None)
        return BumpResult(success=True)

    async def needs_bump(
        self,
        session: AsyncSession,
        lot_id: int,
        interval: timedelta,
    ) -> bool:
        """Проверка: последний успешный bump старее interval (или его не было)."""
        last = await self._last_successful(session, lot_id)
        if last is None:
            return True
        return datetime.now(timezone.utc) - last.bumped_at >= interval

    async def _last_successful(self, session: AsyncSession, lot_id: int) -> BumpLog | None:
        result = await session.execute(
            select(BumpLog)
            .where(BumpLog.lot_id == lot_id, BumpLog.success.is_(True))
            .order_by(desc(BumpLog.bumped_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _log(
        self,
        session: AsyncSession,
        lot_id: int,
        success: bool,
        error: str | None,
    ) -> None:
        entry = BumpLog(
            lot_id=lot_id,
            bumped_at=datetime.now(timezone.utc),
            success=success,
            error=error,
        )
        session.add(entry)
        await session.flush()
