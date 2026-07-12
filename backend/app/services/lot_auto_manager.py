from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration
from app.models.lot import Lot, PriceMatrix
from app.services.lot_sync import LotSyncService


_LIMITS_FRESH_THRESHOLD = timedelta(hours=1)


@dataclass(frozen=True)
class LotAction:
    """Действие, выполненное LotAutoManager над лотом."""

    lot_id: int
    action: str  # create | activate | pause | none


class LotAutoManager:
    """Авто-управление лотами по capacity аккаунтов.

    Для каждой PriceMatrix-связки: есть capacity + лот активен → ничего.
    Есть capacity + лот паушен/отсутствует → активировать/создать.
    Нет capacity + лот активен → паушить.
    """

    def __init__(self, funpay_node_id: int) -> None:
        self._funpay_node_id = funpay_node_id
        self._sync = LotSyncService()

    async def run(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
    ) -> list[LotAction]:
        matrices = await self._load_price_matrices(session)
        actions: list[LotAction] = []

        for matrix in matrices:
            lot = await self._find_lot_for_matrix(session, matrix)
            has_capacity = await self._check_capacity(session, matrix)

            if has_capacity:
                if lot is None:
                    lot = await self._create_lot(session, matrix)
                    await self._sync.sync_lot(session, gateway, lot.id, active=True)
                    actions.append(LotAction(lot_id=lot.id, action="create"))
                elif lot.status == "paused":
                    await self._sync.activate_lot(session, gateway, lot.id)
                    actions.append(LotAction(lot_id=lot.id, action="activate"))
                else:
                    actions.append(LotAction(lot_id=lot.id, action="none"))
            else:
                if lot is not None and lot.status == "active":
                    await self._sync.pause_lot(session, gateway, lot.id)
                    actions.append(LotAction(lot_id=lot.id, action="pause"))
                elif lot is not None:
                    actions.append(LotAction(lot_id=lot.id, action="none"))

        return actions

    async def _load_price_matrices(self, session: AsyncSession) -> list[PriceMatrix]:
        result = await session.execute(select(PriceMatrix))
        return list(result.scalars().all())

    async def _find_lot_for_matrix(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> Lot | None:
        result = await session.execute(
            select(Lot).where(
                Lot.tier_id == matrix.tier_id,
                Lot.duration_id == matrix.duration_id,
                Lot.limit_scope_id == matrix.limit_scope_id,
                Lot.auto_created.is_(True),
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def _check_capacity(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> bool:
        """Есть ли хотя бы один активный аккаунт с подходящим tier и свежими лимитами."""
        now = datetime.now(timezone.utc)
        fresh_cutoff = now - _LIMITS_FRESH_THRESHOLD

        stmt = (
            select(func.count())
            .select_from(Account)
            .join(AccountLimits, AccountLimits.account_id == Account.id)
            .where(
                Account.status == "active",
                Account.tier_id == matrix.tier_id,
                Account.subscription_expires_at >= now,
                AccountLimits.measured_at >= fresh_cutoff,
                AccountLimits.refresh_status == "ok",
            )
        )
        result = await session.execute(stmt)
        count = result.scalar_one()
        return count > 0

    async def _create_lot(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> Lot:
        tier = await session.get(SubscriptionTier, matrix.tier_id)
        duration = await session.get(Duration, matrix.duration_id)
        lot = Lot(
            funpay_node_id=self._funpay_node_id,
            tier_id=matrix.tier_id,
            duration_id=matrix.duration_id,
            limit_scope_id=matrix.limit_scope_id,
            min_limit_pct=matrix.min_limit_pct,
            max_5h_pct=matrix.max_5h_pct,
            max_weekly_pct=matrix.max_weekly_pct,
            price=matrix.price,
            title_ru=self._title(tier, duration, "ru"),
            title_en=self._title(tier, duration, "en"),
            status="active",
            auto_created=True,
        )
        session.add(lot)
        await session.flush()
        return lot

    def _title(self, tier, duration, lang: str) -> str:
        if tier is None or duration is None:
            return "ChatGPT"
        if lang == "ru":
            return f"ChatGPT {tier.name} — {duration.days} дн."
        return f"ChatGPT {tier.name} — {duration.days} days"
