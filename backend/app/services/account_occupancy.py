from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rental import OCCUPYING_RENTAL_STATUSES, Rental


async def active_rental_counts(
    session: AsyncSession,
    account_ids: list[int],
) -> dict[int, int]:
    """Count sold rentals only; replacement targets are not sold yet."""

    if not account_ids:
        return {}
    result = await session.execute(
        select(Rental.account_id, func.count(Rental.id))
        .where(
            Rental.account_id.in_(account_ids),
            Rental.status.in_(OCCUPYING_RENTAL_STATUSES),
        )
        .group_by(Rental.account_id)
    )
    return {
        int(account_id): int(count)
        for account_id, count in result.all()
        if account_id is not None
    }


async def replacement_reserved_account_ids(
    session: AsyncSession,
    account_ids: list[int],
) -> set[int]:
    if not account_ids:
        return set()
    rows = await session.scalars(
        select(Rental.replacement_target_account_id).where(
            Rental.replacement_target_account_id.in_(account_ids)
        )
    )
    return {int(account_id) for account_id in rows if account_id is not None}


async def account_is_busy(session: AsyncSession, account_id: int) -> bool:
    """Protect both the buyer's current account and a reserved replacement."""

    return (
        await session.scalar(
            select(Rental.id)
            .where(
                or_(
                    (
                        (Rental.account_id == account_id)
                        & Rental.status.in_(OCCUPYING_RENTAL_STATUSES)
                    ),
                    Rental.replacement_target_account_id == account_id,
                )
            )
            .limit(1)
        )
    ) is not None
