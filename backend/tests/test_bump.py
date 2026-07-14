from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot, BumpLog
from app.services.bump import BumpService, BumpResult


@pytest.fixture
def gateway() -> FakeChatGateway:
    return FakeChatGateway()


async def _make_lot(session: AsyncSession) -> Lot:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    lot = Lot(
        funpay_id="500",
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="T",
        title_en="T",
        status="active",
        auto_created=True,
    )
    session.add(lot)
    await session.flush()
    return lot


async def test_bump_lot_success(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    svc = BumpService()
    result = await svc.bump_lot(
        session, gateway, lot_id=lot.id, category_id=1, subcategory_id=55,
    )
    assert result.success is True
    assert (1, 55) in gateway.bumped
    logs = (await session.execute(select(BumpLog).where(BumpLog.lot_id == lot.id))).scalars().all()
    assert len(logs) == 1
    assert logs[0].success is True
    assert logs[0].error is None


async def test_bump_lot_records_failure(session: AsyncSession):
    class FailingGateway(FakeChatGateway):
        async def bump_category(self, category_id: int, subcategory_id: int) -> bool:
            raise RuntimeError("network error")

    lot = await _make_lot(session)
    svc = BumpService()
    notifier = AsyncMock()
    with patch(
        "app.services.bump.TelegramNotifier.from_settings",
        new=AsyncMock(return_value=notifier),
    ):
        result = await svc.bump_lot(
            session,
            FailingGateway(),
            lot_id=lot.id,
            category_id=1,
            subcategory_id=55,
        )
    assert result.success is False
    notifier.notify_bump_failed.assert_awaited_once_with(lot.id)
    assert "network error" in (result.error or "")
    logs = (await session.execute(select(BumpLog).where(BumpLog.lot_id == lot.id))).scalars().all()
    assert len(logs) == 1
    assert logs[0].success is False
    assert "network error" in logs[0].error


async def test_needs_bump_no_history(session: AsyncSession):
    lot = await _make_lot(session)
    svc = BumpService()
    needs = await svc.needs_bump(session, lot.id, interval=timedelta(hours=4))
    assert needs is True


async def test_needs_bump_recent_history(session: AsyncSession):
    lot = await _make_lot(session)
    recent = BumpLog(
        lot_id=lot.id,
        bumped_at=datetime.now(timezone.utc) - timedelta(hours=1),
        success=True,
    )
    session.add(recent)
    await session.flush()
    svc = BumpService()
    needs = await svc.needs_bump(session, lot.id, interval=timedelta(hours=4))
    assert needs is False


async def test_needs_bump_old_history(session: AsyncSession):
    lot = await _make_lot(session)
    old = BumpLog(
        lot_id=lot.id,
        bumped_at=datetime.now(timezone.utc) - timedelta(hours=10),
        success=True,
    )
    session.add(old)
    await session.flush()
    svc = BumpService()
    needs = await svc.needs_bump(session, lot.id, interval=timedelta(hours=4))
    assert needs is True
