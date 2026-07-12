from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import Lot, PriceMatrix
from app.models.rental import Rental
from app.models.settings import SellerSettings
from app.services.lot_sync import LotSyncService


_LIMITS_FRESH_THRESHOLD = timedelta(hours=1)


@dataclass(frozen=True)
class LotAction:
    """Action performed by the automatic lot reconciler."""

    lot_id: int
    action: str  # create | activate | pause | update | none


class LotAutoManager:
    """Reconcile FunPay lots with price configuration and real capacity."""

    def __init__(self, funpay_node_id: int) -> None:
        self._funpay_node_id = funpay_node_id
        self._sync = LotSyncService()

    async def run(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
    ) -> list[LotAction]:
        matrices = await self._load_price_matrices(session)
        matrix_keys = {matrix.config_key for matrix in matrices}
        actions: list[LotAction] = []

        # Configurations removed from the matrix must not remain for sale.
        orphaned = await session.execute(
            select(Lot).where(
                Lot.auto_created.is_(True),
                Lot.status != "deleted",
                Lot.config_key.not_in(matrix_keys) if matrix_keys else True,
            )
        )
        for lot in orphaned.scalars().all():
            if lot.status == "active":
                await self._sync.pause_lot(session, gateway, lot.id)
                lot.paused_reason = "auto_no_config"
                actions.append(LotAction(lot.id, "pause"))

        for matrix in matrices:
            lot = await self._find_lot_for_matrix(session, matrix)
            has_capacity = await self._check_capacity(session, matrix)

            if lot is None:
                if not has_capacity:
                    continue
                lot = await self._create_lot(session, matrix)
                await self._sync.sync_lot(session, gateway, lot.id, active=True)
                actions.append(LotAction(lot.id, "create"))
                continue

            changed = self._apply_matrix(lot, matrix)
            automatically_paused = (
                lot.status == "paused"
                and (
                    lot.paused_reason is None  # legacy auto-paused rows
                    or lot.paused_reason.startswith("auto_")
                )
            )

            if has_capacity and automatically_paused:
                if changed:
                    await self._sync.sync_lot(session, gateway, lot.id, active=True)
                else:
                    await self._sync.activate_lot(session, gateway, lot.id)
                lot.status = "active"
                lot.paused_reason = None
                actions.append(LotAction(lot.id, "activate"))
            elif has_capacity and lot.status == "active":
                if changed:
                    await self._sync.sync_lot(session, gateway, lot.id, active=True)
                actions.append(LotAction(lot.id, "update" if changed else "none"))
            elif not has_capacity and lot.status == "active":
                # Full sync updates a changed price and deactivates atomically
                # from the application's perspective.
                if changed:
                    await self._sync.sync_lot(session, gateway, lot.id, active=False)
                else:
                    await self._sync.pause_lot(session, gateway, lot.id)
                lot.status = "paused"
                lot.paused_reason = "auto_no_account"
                actions.append(LotAction(lot.id, "pause"))
            else:
                # A manual pause is never undone by automation.  Keep the
                # remote content current while preserving the inactive state.
                if changed:
                    await self._sync.sync_lot(session, gateway, lot.id, active=False)
                actions.append(LotAction(lot.id, "update" if changed else "none"))

        await session.flush()
        return actions

    async def _load_price_matrices(self, session: AsyncSession) -> list[PriceMatrix]:
        result = await session.execute(
            select(PriceMatrix)
            .join(SubscriptionTier, SubscriptionTier.id == PriceMatrix.tier_id)
            .join(Duration, Duration.id == PriceMatrix.duration_id)
            .where(
                SubscriptionTier.is_active.is_(True),
                SubscriptionTier.is_sellable.is_(True),
                Duration.is_enabled.is_(True),
            )
        )
        return list(result.scalars().all())

    async def _find_lot_for_matrix(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> Lot | None:
        result = await session.execute(
            select(Lot).where(
                Lot.config_key == matrix.config_key,
                Lot.auto_created.is_(True),
                Lot.status != "deleted",
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def _check_capacity(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> bool:
        """Apply the same eligibility rules used during real allocation."""
        duration = await session.get(Duration, matrix.duration_id)
        scope = await session.get(LimitScope, matrix.limit_scope_id)
        tier = await session.get(SubscriptionTier, matrix.tier_id)
        if duration is None or scope is None or tier is None or not tier.is_sellable:
            return False

        settings = await session.get(SellerSettings, 1)
        default_max = settings.default_max_active_rentals if settings else 1
        now = datetime.now(timezone.utc)
        fresh_cutoff = now - _LIMITS_FRESH_THRESHOLD
        required_expires_at = now + timedelta(days=duration.days)
        expiry_condition = Account.subscription_expires_at >= required_expires_at
        if tier.code == "free":
            expiry_condition = or_(
                expiry_condition,
                Account.subscription_expires_at.is_(None),
            )
        active_rentals = (
            select(Rental.account_id, func.count(Rental.id).label("cnt"))
            .where(Rental.status == "active")
            .group_by(Rental.account_id)
            .subquery()
        )

        stmt = (
            select(Account.id)
            .join(AccountLimits, AccountLimits.account_id == Account.id)
            .outerjoin(active_rentals, active_rentals.c.account_id == Account.id)
            .where(
                Account.status == "active",
                Account.tier_id == matrix.tier_id,
                expiry_condition,
                AccountLimits.measured_at >= fresh_cutoff,
                AccountLimits.refresh_status == "ok",
                func.coalesce(Account.max_active_rentals, default_max)
                > func.coalesce(active_rentals.c.cnt, 0),
            )
        )
        if scope.code == "any":
            if matrix.max_5h_pct is not None:
                stmt = stmt.where(
                    AccountLimits.chat_5h_remaining_pct <= matrix.max_5h_pct,
                    AccountLimits.codex_5h_remaining_pct <= matrix.max_5h_pct,
                )
            if matrix.max_weekly_pct is not None:
                stmt = stmt.where(
                    AccountLimits.chat_weekly_remaining_pct <= matrix.max_weekly_pct,
                    AccountLimits.codex_weekly_remaining_pct <= matrix.max_weekly_pct,
                )
        elif scope.code == "chat" and matrix.min_limit_pct is not None:
            stmt = stmt.where(
                AccountLimits.chat_5h_remaining_pct >= matrix.min_limit_pct,
                AccountLimits.chat_weekly_remaining_pct >= matrix.min_limit_pct,
            )
        elif scope.code == "codex" and matrix.min_limit_pct is not None:
            stmt = stmt.where(
                AccountLimits.codex_5h_remaining_pct >= matrix.min_limit_pct,
                AccountLimits.codex_weekly_remaining_pct >= matrix.min_limit_pct,
            )

        result = await session.execute(stmt.limit(1))
        return result.scalar_one_or_none() is not None

    async def _create_lot(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> Lot:
        tier = await session.get(SubscriptionTier, matrix.tier_id)
        duration = await session.get(Duration, matrix.duration_id)
        lot = Lot(
            config_key=matrix.config_key,
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
            paused_reason=None,
            auto_created=True,
        )
        session.add(lot)
        await session.flush()
        return lot

    def _apply_matrix(self, lot: Lot, matrix: PriceMatrix) -> bool:
        changed = (
            lot.price != matrix.price
            or lot.funpay_node_id != self._funpay_node_id
        )
        lot.price = matrix.price
        lot.funpay_node_id = self._funpay_node_id
        return changed

    def _title(self, tier, duration, lang: str) -> str:
        if tier is None or duration is None:
            return "ChatGPT"
        if lang == "ru":
            return f"ChatGPT {tier.name} — {duration.days} дн."
        return f"ChatGPT {tier.name} — {duration.days} days"
