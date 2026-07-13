from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import Lot, PriceMatrix
from app.models.audit import AuditLog
from app.models.rental import Rental
from app.models.settings import SellerSettings
from app.services.limit_eligibility import apply_limit_scope_filters
from app.services.lot_sync import LotSyncService
from app.services.lot_templates import (
    LotTemplateRenderError,
    render_lot_template,
    resolve_lot_template,
)


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
                await session.commit()
                actions.append(LotAction(lot.id, "pause"))

        for matrix in matrices:
            lot = await self._find_lot_for_matrix(session, matrix)
            has_capacity = await self._check_capacity(session, matrix)

            if lot is None:
                if not has_capacity:
                    continue
                try:
                    lot = await self._create_lot(session, matrix)
                except LotTemplateRenderError as exc:
                    await self._handle_template_render_error(
                        session, gateway, matrix, None, exc,
                    )
                    continue
                await self._sync.sync_lot(session, gateway, lot.id, active=True)
                await session.commit()
                actions.append(LotAction(lot.id, "create"))
                continue

            try:
                changed = await self._apply_matrix(session, lot, matrix)
            except LotTemplateRenderError as exc:
                action = await self._handle_template_render_error(
                    session, gateway, matrix, lot, exc,
                )
                if action is not None:
                    actions.append(action)
                continue
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
                await session.commit()
                actions.append(LotAction(lot.id, "activate"))
            elif has_capacity and lot.status == "active":
                if changed:
                    await self._sync.sync_lot(session, gateway, lot.id, active=True)
                    await session.commit()
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
                await session.commit()
                actions.append(LotAction(lot.id, "pause"))
            else:
                # A manual pause is never undone by automation.  Keep the
                # remote content current while preserving the inactive state.
                if changed:
                    await self._sync.sync_lot(session, gateway, lot.id, active=False)
                    await session.commit()
                actions.append(LotAction(lot.id, "update" if changed else "none"))

        await session.flush()
        return actions

    async def _handle_template_render_error(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        matrix: PriceMatrix,
        lot: Lot | None,
        error: LotTemplateRenderError,
    ) -> LotAction | None:
        session.add(
            AuditLog(
                event_type="lot_template_render_failed",
                metadata_={
                    "config_key": matrix.config_key,
                    "error": str(error)[:500],
                },
            )
        )
        action: LotAction | None = None
        if lot is not None and lot.status == "active":
            await self._sync.pause_lot(session, gateway, lot.id)
            lot.status = "paused"
            lot.paused_reason = "auto_template_error"
            action = LotAction(lot.id, "pause")
        await session.commit()
        return action

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
                Account.operator_status_override.is_(None),
                Account.tier_id == matrix.tier_id,
                expiry_condition,
                AccountLimits.measured_at >= fresh_cutoff,
                AccountLimits.refresh_status == "ok",
                AccountLimits.plan_window_status == "ok",
                func.coalesce(Account.max_active_rentals, default_max)
                > func.coalesce(active_rentals.c.cnt, 0),
            )
        )
        stmt = apply_limit_scope_filters(
            stmt,
            scope=scope.code,
            min_limit_pct=matrix.min_limit_pct,
            max_short_pct=matrix.max_5h_pct,
            max_long_pct=matrix.max_weekly_pct,
        )

        result = await session.execute(stmt.limit(1))
        return result.scalar_one_or_none() is not None

    async def _create_lot(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> Lot:
        tier = await session.get(SubscriptionTier, matrix.tier_id)
        duration = await session.get(Duration, matrix.duration_id)
        scope = await session.get(LimitScope, matrix.limit_scope_id)
        title_ru, title_en, description_ru, description_en = (
            await self._render_contents(
                session, tier, duration, scope, matrix,
            )
        )
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
            title_ru=title_ru,
            title_en=title_en,
            description_ru=description_ru,
            description_en=description_en,
            status="active",
            paused_reason=None,
            auto_created=True,
        )
        session.add(lot)
        await session.flush()
        return lot

    async def _apply_matrix(
        self, session: AsyncSession, lot: Lot, matrix: PriceMatrix,
    ) -> bool:
        # Catalog labels and buyer-facing wording may be corrected without a
        # config-key change, so reconciliation must refresh more than price.
        tier = await session.get(SubscriptionTier, matrix.tier_id)
        duration = await session.get(Duration, matrix.duration_id)
        scope = await session.get(LimitScope, matrix.limit_scope_id)
        title_ru, title_en, description_ru, description_en = (
            await self._render_contents(
                session, tier, duration, scope, matrix,
            )
        )
        changed = any((
            lot.price != matrix.price,
            lot.funpay_node_id != self._funpay_node_id,
            lot.title_ru != title_ru,
            lot.title_en != title_en,
            lot.description_ru != description_ru,
            lot.description_en != description_en,
        ))
        lot.price = matrix.price
        lot.funpay_node_id = self._funpay_node_id
        lot.title_ru = title_ru
        lot.title_en = title_en
        lot.description_ru = description_ru
        lot.description_en = description_en
        return changed

    async def _render_contents(
        self,
        session: AsyncSession,
        tier: SubscriptionTier | None,
        duration: Duration | None,
        scope: LimitScope | None,
        matrix: PriceMatrix,
    ) -> tuple[str, str, str, str]:
        if tier is None or duration is None:
            return "ChatGPT", "ChatGPT", "", ""
        template = await resolve_lot_template(
            session,
            tier_id=matrix.tier_id,
            limit_scope_id=matrix.limit_scope_id,
        )
        shared = {
            "plan": tier.name,
            "days": duration.days,
            "long_window_days": 30 if tier.code == "free" else 7,
            "min_limit": (
                f"{matrix.min_limit_pct}%"
                if matrix.min_limit_pct is not None else "—"
            ),
            "short_limit": (
                f"{matrix.max_5h_pct}%"
                if matrix.max_5h_pct is not None else "—"
            ),
            "long_limit": (
                f"{matrix.max_weekly_pct}%"
                if matrix.max_weekly_pct is not None else "—"
            ),
        }
        title_ru, description_ru = render_lot_template(
            template,
            lang="ru",
            variables={
                **shared,
                "condition": self._condition(
                    scope, matrix, "ru", compact=True,
                ),
            },
        )
        title_en, description_en = render_lot_template(
            template,
            lang="en",
            variables={
                **shared,
                "condition": self._condition(
                    scope, matrix, "en", compact=True,
                ),
            },
        )
        return title_ru, title_en, description_ru, description_en

    @staticmethod
    def _condition(scope, matrix: PriceMatrix, lang: str, *, compact: bool) -> str:
        code = scope.code if scope is not None else "any"
        minimum = matrix.min_limit_pct
        if code == "codex" and minimum is not None:
            if compact:
                return f"Codex ≥ {minimum}%"
            return (
                f"остаток во всех наблюдаемых окнах Codex не ниже {minimum}%"
                if lang == "ru"
                else f"at least {minimum}% remaining in every observed Codex window"
            )
        if code == "chat" and minimum is not None:
            if compact:
                return f"ChatGPT ≥ {minimum}%"
            return (
                f"остаток ChatGPT не ниже {minimum}%"
                if lang == "ru"
                else f"at least {minimum}% ChatGPT allowance remaining"
            )
        ceilings = [
            value for value in (matrix.max_5h_pct, matrix.max_weekly_pct)
            if value is not None
        ]
        if code == "any" and ceilings:
            compact_values = "/".join(f"≤ {value}%" for value in ceilings)
            if compact:
                return f"Codex {compact_values}"
            return (
                f"без минимальной гарантии; остаток Codex {compact_values}"
                if lang == "ru"
                else f"no minimum guarantee; Codex remainder {compact_values}"
            )
        if compact:
            return "без гарантии" if lang == "ru" else "no limit guarantee"
        return (
            "без гарантии остатка лимита"
            if lang == "ru"
            else "no remaining-limit guarantee"
        )
