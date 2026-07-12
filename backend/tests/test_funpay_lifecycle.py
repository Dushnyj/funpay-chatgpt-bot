import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.runner import RunnerCallbacks
from app.integrations.funpay.types import MessageInfo, OrderInfo, SaleStatus
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot
from app.models.rental import Order
from app.services.funpay_lifecycle import build_callbacks


async def _seed_lot(session: AsyncSession) -> int:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    lot = Lot(
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
    return lot.id


async def test_build_callbacks_creates_all_handlers(session: AsyncSession):
    gateway = FakeChatGateway()
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    assert isinstance(callbacks, RunnerCallbacks)
    assert callbacks.on_new_sale is not None
    assert callbacks.on_sale_closed is not None
    assert callbacks.on_sale_refunded is not None
    assert callbacks.on_message is not None


async def test_on_new_sale_callback_processes_order(session: AsyncSession):
    await _seed_lot(session)
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="ord-1",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="test",
        price=599.0,
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    await callbacks.on_new_sale("ord-1")  # type: ignore
    result = await session.execute(select(Order).where(Order.funpay_order_id == "ord-1"))
    assert result.scalar_one_or_none() is not None


async def test_on_sale_closed_callback_updates_status(session: AsyncSession):
    await _seed_lot(session)
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="ord-1",
        status=SaleStatus.COMPLETED,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="test",
        price=599.0,
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    await callbacks.on_new_sale("ord-1")  # type: ignore
    await callbacks.on_sale_closed("ord-1")  # type: ignore
    order = await session.get(Order, 1)
    assert order.status == "completed"


async def test_on_message_callback_dispatches_command(session: AsyncSession):
    gateway = FakeChatGateway()
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    msg = MessageInfo(
        message_id=1,
        chat_id=100,
        sender_id=200,
        text="!помощь",
        order_id="ord-1",
    )
    # Распознанная команда без зарегистрированного хэндлера → UnhandledMessage,
    # но lifecycle ловит и логирует (не падает)
    await callbacks.on_message(msg)  # type: ignore
