from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Duration, LimitScope, SubscriptionTier


class OfferConfiguration(Protocol):
    tier_id: int
    duration_id: int
    limit_scope_id: int
    min_limit_pct: int | None
    max_5h_pct: int | None
    max_weekly_pct: int | None


class OfferConfigurationError(ValueError):
    """A price/lot condition cannot be represented truthfully."""


async def validate_offer_configurations(
    session: AsyncSession,
    items: Sequence[OfferConfiguration],
    *,
    require_sellable_tier: bool = True,
    require_enabled_duration: bool = True,
    require_enabled_scope: bool = True,
) -> None:
    if not items:
        return

    tier_ids = {item.tier_id for item in items}
    duration_ids = {item.duration_id for item in items}
    scope_ids = {item.limit_scope_id for item in items}
    tiers = {
        tier.id: tier
        for tier in (
            await session.execute(
                select(SubscriptionTier).where(SubscriptionTier.id.in_(tier_ids))
            )
        ).scalars()
    }
    durations = {
        duration.id: duration
        for duration in (
            await session.execute(select(Duration).where(Duration.id.in_(duration_ids)))
        ).scalars()
    }
    scopes = {
        scope.id: scope
        for scope in (
            await session.execute(select(LimitScope).where(LimitScope.id.in_(scope_ids)))
        ).scalars()
    }

    seen: set[tuple[int, int, int, int | None, int | None, int | None]] = set()
    for index, item in enumerate(items, start=1):
        tier = tiers.get(item.tier_id)
        duration = durations.get(item.duration_id)
        scope = scopes.get(item.limit_scope_id)
        if tier is None or (
            require_sellable_tier
            and (not tier.is_active or not tier.is_sellable)
        ):
            raise OfferConfigurationError(
                f"Price row {index}: tariff is unavailable for sale"
            )
        if duration is None or (
            require_enabled_duration and not duration.is_enabled
        ):
            raise OfferConfigurationError(
                f"Price row {index}: duration is disabled or missing"
            )
        if (
            scope is None
            or scope.code not in {"any", "codex"}
            or (require_enabled_scope and not scope.is_enabled)
        ):
            raise OfferConfigurationError(
                f"Price row {index}: limit scope is disabled or invalid"
            )
        if scope.code == "any" and item.min_limit_pct is not None:
            raise OfferConfigurationError(
                f"Price row {index}: any scope cannot promise a minimum limit"
            )
        if (
            scope.code == "any"
            and tier.code == "free"
            and item.max_5h_pct is not None
        ):
            raise OfferConfigurationError(
                f"Price row {index}: Free has no observed 5-hour window; "
                "clear max_5h_pct"
            )
        if scope.code == "codex":
            if item.min_limit_pct is None:
                raise OfferConfigurationError(
                    f"Price row {index}: guaranteed scope requires a minimum limit"
                )
            if item.max_5h_pct is not None or item.max_weekly_pct is not None:
                raise OfferConfigurationError(
                    f"Price row {index}: guaranteed scope cannot use maximum ceilings"
                )

        signature = (
            item.tier_id,
            item.duration_id,
            item.limit_scope_id,
            item.min_limit_pct,
            item.max_5h_pct,
            item.max_weekly_pct,
        )
        if signature in seen:
            raise OfferConfigurationError(
                f"Price row {index}: duplicate configuration"
            )
        seen.add(signature)
