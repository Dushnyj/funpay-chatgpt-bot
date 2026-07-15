from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.catalog import SubscriptionTier
from app.models.rental import OCCUPYING_RENTAL_STATUSES, Rental
from app.services.account_occupancy import account_is_busy
from app.services.delivery_policy import DELIVERY_ALLOCATION_HEADROOM
from app.services.limit_eligibility import (
    apply_limit_scope_filters,
    observed_codex_long,
)
from app.services.subscription_eligibility import (
    trusted_paid_subscription_expiry,
)


_LIMITS_FRESH_THRESHOLD = timedelta(hours=1)
def limits_freshness_for_duration(duration_minutes: int) -> timedelta:
    """Short rentals require proportionally fresher measured usage."""

    return min(
        _LIMITS_FRESH_THRESHOLD,
        max(
            timedelta(minutes=5),
            timedelta(minutes=duration_minutes / 2),
        ),
    )


@dataclass(frozen=True)
class AccountCriteria:
    """Критерии выбора аккаунта для выдачи под заказ."""

    tier_id: int
    duration_minutes: int
    scope: str  # any | codex
    min_limit_pct: int | None
    max_5h_pct: int | None
    max_weekly_pct: int | None
    # Replacement keeps the original rental deadline rather than requiring a
    # fresh full catalog term. When omitted, allocation reserves retry headroom.
    required_expires_at: datetime | None = None


class AccountPool:
    """Выбор аккаунта из пула под критерии заказа.

    base_filter: status=active, tier, подписка >= duration, лимиты свежие, refresh ok,
                 активных аренд < эффективного лимита.
    scope=any: optional ceiling applies to the verified long Codex window.
    scope=codex: the verified long Codex window must meet the guarantee.
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
        freshness = limits_freshness_for_duration(criteria.duration_minutes)
        fresh_cutoff = now - freshness
        required_expires_at = criteria.required_expires_at
        if required_expires_at is None:
            required_expires_at = (
                now
                + timedelta(minutes=criteria.duration_minutes)
                + DELIVERY_ALLOCATION_HEADROOM
            )
        elif required_expires_at.tzinfo is None:
            required_expires_at = required_expires_at.replace(
                tzinfo=timezone.utc
            )

        active_rentals = (
            select(
                Rental.account_id,
                func.count(Rental.id).label("cnt"),
            )
            .where(Rental.status.in_(OCCUPYING_RENTAL_STATUSES))
            .group_by(Rental.account_id)
            .subquery()
        )
        reserved_replacement_targets = select(
            Rental.replacement_target_account_id
        ).where(Rental.replacement_target_account_id.is_not(None))
        active_checks = select(AccountCheckJob.account_id).where(
            AccountCheckJob.status.in_(["pending", "running"])
        )

        stmt = (
            select(Account)
            .join(AccountLimits, AccountLimits.account_id == Account.id)
            .join(SubscriptionTier, SubscriptionTier.id == Account.tier_id)
            .outerjoin(active_rentals, active_rentals.c.account_id == Account.id)
            .where(
                Account.status == "active",
                Account.operator_status_override.is_(None),
                Account.tier_id == criteria.tier_id,
                SubscriptionTier.is_active.is_(True),
                SubscriptionTier.is_sellable.is_(True),
                or_(
                    and_(
                        SubscriptionTier.code != "free",
                        trusted_paid_subscription_expiry(
                            required_expires_at
                        ),
                    ),
                    and_(
                        SubscriptionTier.code == "free",
                        Account.subscription_expires_at.is_(None),
                    ),
                ),
                AccountLimits.measured_at >= fresh_cutoff,
                AccountLimits.refresh_status == "ok",
                AccountLimits.plan_window_status == "ok",
                # OpenAI logout is account-wide, so independent renters can
                # never be isolated safely on one credential set.
                func.coalesce(active_rentals.c.cnt, 0) < 1,
                # Replacement reserves its exact target durably before the
                # old account is logged out. Neither a normal sale nor another
                # replacement may allocate that promised target meanwhile.
                Account.id.not_in(reserved_replacement_targets),
                # Refresh/validation workers publish ``running`` while holding
                # the same Account lock. Do not allocate credentials that a
                # live worker can still invalidate before delivery.
                Account.id.not_in(active_checks),
            )
        )
        if exclude_account_id is not None:
            stmt = stmt.where(Account.id != exclude_account_id)

        stmt = apply_limit_scope_filters(
            stmt,
            scope=criteria.scope,
            min_limit_pct=criteria.min_limit_pct,
            max_short_pct=criteria.max_5h_pct,
            max_long_pct=criteria.max_weekly_pct,
        )
        if criteria.scope == "any":
            stmt = stmt.order_by(Account.subscription_expires_at.asc())
        else:  # codex (unknown scopes were made unsellable above)
            stmt = stmt.order_by(observed_codex_long().desc())

        # The row lock is held by the caller's transaction until it creates the
        # Rental and commits. Concurrent workers skip the selected account
        # instead of issuing the same final slot twice.
        #
        # PostgreSQL READ COMMITTED takes one snapshot per statement. A job or
        # rental can be committed after the cross-table predicates above were
        # evaluated but immediately before ``LockRows`` acquires Account. The
        # Account tuple itself is not updated in that race, so EvalPlanQual
        # cannot make those subqueries fresh. Recheck in a second statement
        # after owning Account and retry another candidate on conflict.
        rejected_account_ids: set[int] = set()
        while True:
            candidate_stmt = stmt
            if rejected_account_ids:
                candidate_stmt = candidate_stmt.where(
                    Account.id.not_in(rejected_account_ids)
                )
            candidate_stmt = candidate_stmt.limit(1).with_for_update(
                of=Account,
                skip_locked=True,
            )
            account = (
                await session.execute(candidate_stmt)
            ).scalar_one_or_none()
            if account is None:
                return None
            if await _has_fresh_allocation_conflict(session, account.id):
                rejected_account_ids.add(account.id)
                continue
            return account

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


async def _has_fresh_allocation_conflict(
    session: AsyncSession,
    account_id: int,
) -> bool:
    """Recheck mutable cross-table blockers after Account is locked."""

    if await account_is_busy(session, account_id):
        return True
    return (
        await session.scalar(
            select(AccountCheckJob.id)
            .where(
                AccountCheckJob.account_id == account_id,
                AccountCheckJob.status.in_(["pending", "running"]),
            )
            .limit(1)
        )
    ) is not None
