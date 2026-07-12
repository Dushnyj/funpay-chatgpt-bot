import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import OrderInfo, SaleStatus
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot
from app.models.rental import Order
from app.services.order_processor import OrderProcessor, LotNotFoundError


@pytest.fixture
def gateway() -> FakeChatGateway:
    gw = FakeChatGateway()
    gw.set_order(OrderInfo(
        order_id="ord-1",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="ChatGPT Plus 7d",
        price=599.0,
    ))
    return gw


async def _seed_catalog_and_lot(session: AsyncSession, funpay_node_id: int = 55) -> int:
    """Создаёт tier+duration+scope+lot и возвращает lot_id."""
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    lot = Lot(
        funpay_node_id=funpay_node_id,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Plus 7d",
        title_en="Plus 7d",
        status="active",
        auto_created=True,
    )
    session.add(lot)
    await session.flush()
    return lot.id


async def test_process_new_sale_creates_order(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    order = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert order.funpay_order_id == "ord-1"
    assert order.funpay_chat_id == "100"
    assert order.buyer_funpay_id == "200"
    assert order.lot_id is not None
    assert order.status == "pending"


async def test_process_new_sale_idempotent(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    first = await proc.process_new_sale(session, gateway, order_id="ord-1")
    second = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert first.id == second.id
    result = await session.execute(select(Order).where(Order.funpay_order_id == "ord-1"))
    assert len(result.scalars().all()) == 1


async def test_process_new_sale_no_matching_lot_raises(
    session: AsyncSession, gateway: FakeChatGateway,
):
    proc = OrderProcessor()
    with pytest.raises(LotNotFoundError):
        await proc.process_new_sale(session, gateway, order_id="ord-1")


async def test_process_sale_closed_marks_completed(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    await proc.process_new_sale(session, gateway, order_id="ord-1")
    order = await proc.process_sale_closed(session, order_id="ord-1")
    assert order.status == "completed"


async def test_process_sale_refunded_marks_refunded(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    await proc.process_new_sale(session, gateway, order_id="ord-1")
    order = await proc.process_sale_refunded(session, order_id="ord-1")
    assert order.status == "refunded"


async def test_process_sale_closed_unknown_order_raises(session: AsyncSession):
    proc = OrderProcessor()
    with pytest.raises(KeyError):
        await proc.process_sale_closed(session, order_id="nope")


async def test_process_new_sale_records_tier_duration_scope(
    session: AsyncSession, gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    order = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert order.tier_id is not None
    assert order.duration_id is not None
    assert order.limit_scope_id is not None
