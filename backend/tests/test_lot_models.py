from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import BumpLog, Lot, PriceMatrix


@pytest.mark.asyncio
async def test_create_lot_with_all_threshold_fields(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(days=7, is_enabled=True, sort_order=1)
    scope = LimitScope(code="codex", name="Codex")
    session.add_all([tier, dur, scope])
    await session.flush()

    lot = Lot(
        funpay_node_id=12345,
        tier_id=tier.id,
        duration_id=dur.id,
        limit_scope_id=scope.id,
        min_limit_pct=50,
        max_5h_pct=None,
        max_weekly_pct=None,
        price=599,
        title_ru="ChatGPT Plus — 7 дн. (Codex ≥50%)",
        title_en="ChatGPT Plus — 7 days (Codex ≥50%)",
        description_ru="...",
        description_en="...",
        auto_created=True,
    )
    session.add(lot)
    await session.commit()

    fetched = await session.get(Lot, lot.id)
    assert fetched.status == "active"
    assert fetched.paused_reason is None
    assert fetched.min_limit_pct == 50
    assert fetched.funpay_id is None  # не создан на FunPay ещё


@pytest.mark.asyncio
async def test_price_matrix_unique_constraint(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(days=7, is_enabled=True)
    scope_any = LimitScope(code="any", name="Любой")
    session.add_all([tier, dur, scope_any])
    await session.flush()

    pm1 = PriceMatrix(
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope_any.id,
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None, price=299,
    )
    session.add(pm1)
    await session.flush()

    pm2 = PriceMatrix(
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope_any.id,
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None, price=399,
    )
    session.add(pm2)
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_bump_log_created(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(days=7, is_enabled=True)
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, dur, scope])
    await session.flush()

    lot = Lot(
        funpay_node_id=1, tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        price=299, title_ru="t", title_en="t", description_ru="", description_en="",
    )
    session.add(lot)
    await session.flush()

    bump = BumpLog(lot_id=lot.id, bumped_at=datetime.now(timezone.utc), success=True)
    session.add(bump)
    await session.commit()

    fetched = await session.execute(select(BumpLog).where(BumpLog.lot_id == lot.id))
    assert fetched.scalar_one().success is True
