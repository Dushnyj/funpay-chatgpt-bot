from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier
from app.models.rental import Rental


_LIMITS_FRESH_THRESHOLD = timedelta(hours=1)


@dataclass(frozen=True)
class AccountCriteria:
    """Критерии выбора аккаунта для выдачи под заказ."""

    tier_id: int
    duration_days: int
    scope: str  # any | chat | codex
    min_limit_pct: int | None
    max_5h_pct: int | None
    max_weekly_pct: int | None


class AccountPool:
    """Выбор аккаунта из пула под критерии заказа.

    base_filter: status=active, tier, подписка >= duration, лимиты свежие, refresh ok,
                 активных аренд < эффективного лимита.
    scope=any: потолок (если задан) — все 4 замера ≤ порогов. FIFO по подписке.
    scope=chat/codex: гарантия — оба окна типа ≥ min_limit_pct. Наибольший запас.
    """

    async def acquire(
        self,
        session: AsyncSession,
        criteria: AccountCriteria,
        default_max_active_rentals: int,
    ) -> Account | None:
        return await self._acquire(
            session, criteria, default_max_active_rentals, exclude_account_id=None
        )

    async def _acquire(
        self,
        session: AsyncSession,
        criteria: AccountCriteria,
        default_max_active_rentals: int,
        *,
        exclude_account_id: int | None,
    ) -> Account | None:
        now = datetime.now(timezone.utc)
        fresh_cutoff = now - _LIMITS_FRESH_THRESHOLD
        required_expires_at = now + timedelta(days=criteria.duration_days)

        active_rentals = (
            select(
                Rental.account_id,
                func.count(Rental.id).label("cnt"),
            )
            .where(Rental.status == "active")
            .group_by(Rental.account_id)
            .subquery()
        )

        stmt = (
            select(Account)
            .join(AccountLimits, AccountLimits.account_id == Account.id)
            .join(SubscriptionTier, SubscriptionTier.id == Account.tier_id)
            .outerjoin(active_rentals, active_rentals.c.account_id == Account.id)
            .where(
                Account.status == "active",
                Account.tier_id == criteria.tier_id,
                or_(
                    Account.subscription_expires_at >= required_expires_at,
                    and_(
                        SubscriptionTier.code == "free",
                        Account.subscription_expires_at.is_(None),
                    ),
                ),
                AccountLimits.measured_at >= fresh_cutoff,
                AccountLimits.refresh_status == "ok",
                func.coalesce(
                    Account.max_active_rentals, default_max_active_rentals
                )
                > func.coalesce(active_rentals.c.cnt, 0),
            )
        )
        if exclude_account_id is not None:
            stmt = stmt.where(Account.id != exclude_account_id)

        if criteria.scope == "any":
            if criteria.max_5h_pct is not None:
                stmt = stmt.where(
                    AccountLimits.chat_5h_remaining_pct <= criteria.max_5h_pct,
                    AccountLimits.codex_5h_remaining_pct <= criteria.max_5h_pct,
                )
            if criteria.max_weekly_pct is not None:
                stmt = stmt.where(
                    AccountLimits.chat_weekly_remaining_pct <= criteria.max_weekly_pct,
                    AccountLimits.codex_weekly_remaining_pct <= criteria.max_weekly_pct,
                )
            stmt = stmt.order_by(Account.subscription_expires_at.asc())
        elif criteria.scope == "chat":
            if criteria.min_limit_pct is not None:
                stmt = stmt.where(
                    AccountLimits.chat_5h_remaining_pct >= criteria.min_limit_pct,
                    AccountLimits.chat_weekly_remaining_pct >= criteria.min_limit_pct,
                )
            stmt = stmt.order_by(
                _lower_limit(
                    AccountLimits.chat_5h_remaining_pct,
                    AccountLimits.chat_weekly_remaining_pct,
                ).desc()
            )
        else:  # codex
            if criteria.min_limit_pct is not None:
                stmt = stmt.where(
                    AccountLimits.codex_primary_remaining_pct
                    >= criteria.min_limit_pct,
                    or_(
                        AccountLimits.codex_secondary_remaining_pct.is_(None),
                        AccountLimits.codex_secondary_remaining_pct
                        >= criteria.min_limit_pct,
                    ),
                )
            stmt = stmt.order_by(
                _lower_optional_limit(
                    AccountLimits.codex_primary_remaining_pct,
                    AccountLimits.codex_secondary_remaining_pct,
                ).desc()
            )

        # The row lock is held by the caller's transaction until it creates the
        # Rental and commits. Concurrent workers skip the selected account
        # instead of issuing the same final slot twice.
        stmt = stmt.limit(1).with_for_update(of=Account, skip_locked=True)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def acquire_excluding(
        self,
        session: AsyncSession,
        criteria: AccountCriteria,
        exclude_account_id: int,
        default_max_active_rentals: int,
    ) -> Account | None:
        """Like ``acquire``, excluding the current account without mutation."""
        return await self._acquire(
            session,
            criteria,
            default_max_active_rentals,
            exclude_account_id=exclude_account_id,
        )


def _lower_limit(left, right):
    """Portable scalar minimum (SQLite and PostgreSQL)."""
    return case((left <= right, left), else_=right)


def _lower_optional_limit(primary, secondary):
    """Minimum of observed Codex windows, allowing absent secondary data."""
    return case(
        (secondary.is_(None), primary),
        (primary <= secondary, primary),
        else_=secondary,
    )
