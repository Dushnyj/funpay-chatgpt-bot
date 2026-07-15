from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import Lot, PriceMatrix
from app.models.audit import AuditLog
from app.models.rental import OCCUPYING_RENTAL_STATUSES, Rental
from app.services.limit_eligibility import apply_limit_scope_filters
from app.services.durations import format_duration, format_legacy_days
from app.services.account_pool import (
    DELIVERY_ALLOCATION_HEADROOM,
    limits_freshness_for_duration,
)
from app.services.lot_sync import LotSyncService
from app.services.funpay_offer_mapping import SUPPORTED_FUNPAY_TIER_CODES
from app.services.lot_templates import (
    LotTemplateRenderError,
    render_lot_template,
    resolve_lot_template,
)
from app.services.subscription_eligibility import (
    trusted_paid_subscription_expiry,
)


logger = logging.getLogger(__name__)
_AUTO_PUBLISH_PENDING = "auto_publish_pending"


class ProvenanceMarkerSyncError(RuntimeError):
    """One or more active published lots still lack the ownership marker."""


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
        *,
        refresh_stock: bool = False,
        refresh_published: bool = False,
    ) -> list[LotAction]:
        actions = await self._pause_catalog_invalid_lots(session, gateway)
        marker_actions = await self.sync_missing_provenance_markers(
            session,
            gateway,
        )
        actions.extend(marker_actions)
        # A marker migration is already a complete offer save, so the forced
        # refresh pass below need not immediately repeat the same request.
        full_synced_lot_ids = {
            action.lot_id
            for action in marker_actions
            if action.action == "update"
        }
        if self._funpay_node_id <= 0:
            # Without a configured global node we can still make unsafe offers
            # unavailable, but must not apply a matrix to otherwise valid auto
            # lots: _apply_matrix would replace their per-lot node with zero.
            # A forced refresh remains safe because it reuses every published
            # lot's own node and exact bound offer id.
            if refresh_published:
                refreshed = await self._force_refresh_published_lots(
                    session,
                    gateway,
                    skip_lot_ids=full_synced_lot_ids,
                )
                self._merge_forced_refresh_actions(actions, refreshed)
            await session.flush()
            return actions
        matrices = await self._load_price_matrices(session)
        matrix_keys = {matrix.config_key for matrix in matrices}

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
                if not has_capacity or self._funpay_node_id <= 0:
                    continue
                try:
                    lot = await self._create_lot(session, matrix)
                except LotTemplateRenderError as exc:
                    await self._handle_template_render_error(
                        session, gateway, matrix, None, exc,
                    )
                    continue
                # Persist the random ownership token before crossing the
                # network boundary. The remote offer is created inactive,
                # then its exact ID binding is committed before activation.
                # A crash at any point therefore leaves a recoverable local
                # identity and never an unbound offer available for sale.
                await session.commit()
                await self._publish_pending_lot(session, gateway, lot)
                full_synced_lot_ids.add(lot.id)
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
                if lot.paused_reason == _AUTO_PUBLISH_PENDING:
                    await self._publish_pending_lot(session, gateway, lot)
                    full_synced_lot_ids.add(lot.id)
                    actions.append(LotAction(lot.id, "activate"))
                    continue
                if changed or refresh_stock:
                    await self._sync.sync_lot(session, gateway, lot.id, active=True)
                    full_synced_lot_ids.add(lot.id)
                else:
                    await self._sync.activate_lot(session, gateway, lot.id)
                lot.status = "active"
                lot.paused_reason = None
                await session.commit()
                actions.append(LotAction(lot.id, "activate"))
            elif has_capacity and lot.status == "active":
                if changed or refresh_stock:
                    await self._sync.sync_lot(session, gateway, lot.id, active=True)
                    full_synced_lot_ids.add(lot.id)
                    await session.commit()
                actions.append(LotAction(
                    lot.id,
                    "update" if changed or refresh_stock else "none",
                ))
            elif not has_capacity and lot.status == "active":
                # Full sync updates a changed price and deactivates atomically
                # from the application's perspective.
                if changed or refresh_stock:
                    await self._sync.sync_lot(session, gateway, lot.id, active=False)
                    full_synced_lot_ids.add(lot.id)
                else:
                    await self._sync.pause_lot(session, gateway, lot.id)
                lot.status = "paused"
                lot.paused_reason = "auto_no_account"
                await session.commit()
                actions.append(LotAction(lot.id, "pause"))
            else:
                # A manual pause is never undone by automation.  Keep the
                # remote content current while preserving the inactive state.
                if changed or refresh_stock:
                    if lot.funpay_id:
                        await self._sync.sync_lot(
                            session,
                            gateway,
                            lot.id,
                            active=False,
                        )
                        full_synced_lot_ids.add(lot.id)
                    await session.commit()
                actions.append(LotAction(
                    lot.id,
                    "update" if changed or refresh_stock else "none",
                ))

        if refresh_published:
            # Matrix reconciliation above covers configured auto lots. This
            # final pass is deliberately broader: payment-message content is
            # not stored on Lot, and published manual/orphaned/paused offers
            # would otherwise never receive an edited payment_received
            # template. Every save uses the existing offer id and the final
            # safe local status, so it cannot create an offer or reanimate a
            # lot paused by catalog/capacity safety checks.
            refreshed = await self._force_refresh_published_lots(
                session,
                gateway,
                skip_lot_ids=full_synced_lot_ids,
            )
            self._merge_forced_refresh_actions(actions, refreshed)

        await session.flush()
        return actions

    async def _force_refresh_published_lots(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        *,
        skip_lot_ids: set[int],
    ) -> list[int]:
        """Full-sync every bound supported offer without changing its status."""

        result = await session.execute(
            select(Lot.id, Lot.status)
            .join(SubscriptionTier, SubscriptionTier.id == Lot.tier_id)
            .where(
                Lot.funpay_id.is_not(None),
                Lot.status.in_(("active", "paused")),
                SubscriptionTier.code.in_(SUPPORTED_FUNPAY_TIER_CODES),
            )
            .order_by(Lot.id)
        )
        refreshed: list[int] = []
        for lot_id, status in result.all():
            if lot_id in skip_lot_ids:
                continue
            await self._sync.sync_lot(
                session,
                gateway,
                lot_id,
                active=status == "active",
            )
            await session.commit()
            refreshed.append(lot_id)
        return refreshed

    @staticmethod
    def _merge_forced_refresh_actions(
        actions: list[LotAction],
        refreshed_lot_ids: list[int],
    ) -> None:
        """Expose one meaningful action per lot after the final full save."""

        for lot_id in refreshed_lot_ids:
            existing_index = next(
                (
                    index
                    for index, action in enumerate(actions)
                    if action.lot_id == lot_id
                ),
                None,
            )
            if existing_index is None:
                actions.append(LotAction(lot_id, "update"))
            elif actions[existing_index].action == "none":
                actions[existing_index] = LotAction(lot_id, "update")

    async def _publish_pending_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot: Lot,
    ) -> None:
        """Bind one inactive remote offer durably, then make it sellable."""

        await self._sync.sync_lot(session, gateway, lot.id, active=False)
        lot.status = "paused"
        lot.paused_reason = _AUTO_PUBLISH_PENDING
        # This is the safety boundary: after it, every remotely activatable
        # offer has a durable exact local ID/token binding.
        await session.commit()
        await self._sync.activate_lot(session, gateway, lot.id)
        lot.paused_reason = None
        await session.commit()

    async def sync_missing_provenance_markers(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        *,
        strict: bool = False,
    ) -> list[LotAction]:
        """Publish pre-migration markers with per-lot transaction isolation.

        Periodic reconciliation logs and retries individual failures. Startup
        uses ``strict=True`` and will not enable the listener while an active
        sellable lot remains unmarked.
        """

        result = await session.execute(
            select(Lot.id, Lot.status)
            .join(SubscriptionTier, SubscriptionTier.id == Lot.tier_id)
            .where(
                Lot.funpay_id.is_not(None),
                Lot.status != "deleted",
                Lot.provenance_marker_synced.is_(False),
                SubscriptionTier.code.in_(SUPPORTED_FUNPAY_TIER_CODES),
            )
        )
        actions: list[LotAction] = []
        failed_active: list[int] = []
        for lot_id, status in result.all():
            try:
                await self._sync.sync_lot(
                    session,
                    gateway,
                    lot_id,
                    active=status == "active",
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "Failed to publish provenance marker for lot %s",
                    lot_id,
                )
                if status == "active":
                    failed_active.append(lot_id)
                continue
            actions.append(LotAction(lot_id, "update"))
        if strict and failed_active:
            raise ProvenanceMarkerSyncError(
                "Active lots without a synced provenance marker: "
                + ", ".join(str(item) for item in failed_active)
            )
        return actions

    async def _pause_catalog_invalid_lots(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
    ) -> list[LotAction]:
        """Pause published offers whose catalog contract is no longer valid.

        This applies to manual lots as well as generated ones. Disabling a
        catalog item is an operator safety switch, not merely a hint for new
        automatic lots, so an already-published manual offer cannot stay live.
        """
        result = await session.execute(
            select(Lot)
            .join(SubscriptionTier, SubscriptionTier.id == Lot.tier_id)
            .join(Duration, Duration.id == Lot.duration_id)
            .join(LimitScope, LimitScope.id == Lot.limit_scope_id)
            .where(
                Lot.status == "active",
                or_(
                    SubscriptionTier.is_active.is_(False),
                    SubscriptionTier.is_sellable.is_(False),
                    SubscriptionTier.code.is_(None),
                    SubscriptionTier.code.not_in(SUPPORTED_FUNPAY_TIER_CODES),
                    Duration.is_enabled.is_(False),
                    LimitScope.is_enabled.is_(False),
                    LimitScope.code.not_in(("any", "codex")),
                ),
            )
        )
        actions: list[LotAction] = []
        for lot in result.scalars().all():
            if lot.funpay_id:
                await self._sync.pause_lot(session, gateway, lot.id)
            else:
                # A local active row without a remote id is inconsistent but
                # can still be made safe without making a remote call.
                lot.status = "paused"
                await session.flush()
            lot.paused_reason = (
                "auto_no_config" if lot.auto_created else "catalog_unavailable"
            )
            await session.commit()
            actions.append(LotAction(lot.id, "pause"))
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
            .join(LimitScope, LimitScope.id == PriceMatrix.limit_scope_id)
            .where(
                SubscriptionTier.is_active.is_(True),
                SubscriptionTier.is_sellable.is_(True),
                SubscriptionTier.code.in_(SUPPORTED_FUNPAY_TIER_CODES),
                Duration.is_enabled.is_(True),
                LimitScope.is_enabled.is_(True),
                LimitScope.code.in_(("any", "codex")),
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
        if (
            duration is None
            or scope is None
            or tier is None
            or not tier.is_sellable
            or not scope.is_enabled
            or scope.code not in {"any", "codex"}
        ):
            return False

        now = datetime.now(timezone.utc)
        fresh_cutoff = now - limits_freshness_for_duration(duration.minutes)
        required_expires_at = (
            now
            + timedelta(minutes=duration.minutes)
            + DELIVERY_ALLOCATION_HEADROOM
        )
        expiry_condition = trusted_paid_subscription_expiry(
            required_expires_at
        )
        if tier.code == "free":
            expiry_condition = or_(
                expiry_condition,
                Account.subscription_expires_at.is_(None),
            )
        active_rentals = (
            select(Rental.account_id, func.count(Rental.id).label("cnt"))
            .where(Rental.status.in_(OCCUPYING_RENTAL_STATUSES))
            .group_by(Rental.account_id)
            .subquery()
        )
        reserved_for_replacement = (
            select(Rental.id)
            .where(Rental.replacement_target_account_id == Account.id)
            .exists()
        )
        active_checks = select(AccountCheckJob.account_id).where(
            AccountCheckJob.status.in_(("pending", "running"))
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
                func.coalesce(active_rentals.c.cnt, 0) < 1,
                ~reserved_for_replacement,
                Account.id.not_in(active_checks),
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
            # Kept in the schema for legacy compatibility only. New offers
            # expose and sell the verified plan-specific long Codex window.
            max_5h_pct=None,
            max_weekly_pct=matrix.max_weekly_pct,
            price=matrix.price,
            title_ru=title_ru,
            title_en=title_en,
            description_ru=description_ru,
            description_en=description_en,
            status="paused",
            paused_reason=_AUTO_PUBLISH_PENDING,
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
            lot.min_limit_pct != matrix.min_limit_pct,
            lot.max_5h_pct is not None,
            lot.max_weekly_pct != matrix.max_weekly_pct,
        ))
        lot.price = matrix.price
        lot.funpay_node_id = self._funpay_node_id
        lot.title_ru = title_ru
        lot.title_en = title_en
        lot.description_ru = description_ru
        lot.description_en = description_en
        lot.min_limit_pct = matrix.min_limit_pct
        lot.max_5h_pct = None
        lot.max_weekly_pct = matrix.max_weekly_pct
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
        if duration.minutes % (24 * 60) != 0 and template is not None:
            contents = (
                template.title_template_ru,
                template.title_template_en,
                template.description_template_ru,
                template.description_template_en,
            )
            if any("{days}" in content for content in contents):
                raise LotTemplateRenderError(
                    "Sub-day rentals require {duration}; update the custom "
                    "template that still uses deprecated {days}"
                )
        shared = {
            "plan": tier.name,
            "duration_minutes": duration.minutes,
            "days": format_legacy_days(duration.minutes),
            "long_window_days": 30 if tier.code == "free" else 7,
            "min_limit": (
                f"{matrix.min_limit_pct}%"
                if matrix.min_limit_pct is not None else "—"
            ),
            "short_limit": (
                # Legacy template compatibility only: the short window is no
                # longer a sellable or buyer-visible condition.
                "—"
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
                "duration": format_duration(duration.minutes, "ru"),
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
                "duration": format_duration(duration.minutes, "en"),
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
                f"остаток длинного окна Codex не ниже {minimum}%"
                if lang == "ru"
                else f"at least {minimum}% remaining in the long Codex window"
            )
        long_ceiling = matrix.max_weekly_pct
        if code == "any" and long_ceiling is not None:
            compact_value = f"≤ {long_ceiling}%"
            if compact:
                return f"Codex {compact_value}"
            return (
                f"без минимальной гарантии; длинный лимит Codex {compact_value}"
                if lang == "ru"
                else f"no minimum guarantee; long Codex allowance {compact_value}"
            )
        if compact:
            return "без гарантии" if lang == "ru" else "no limit guarantee"
        return (
            "без гарантии остатка лимита"
            if lang == "ru"
            else "no remaining-limit guarantee"
        )
