import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.runner import RunnerCallbacks
from app.integrations.funpay.types import MessageInfo, OrderInfo, SaleStatus
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.chat import ChatConversation, ChatMessage
from app.models.lot import Lot
from app.models.rental import Order
from app.services.funpay_lifecycle import build_callbacks


async def _seed_lot(session: AsyncSession) -> int:
    tier = SubscriptionTier(
        code="plus", name="Plus", is_active=True, is_sellable=True,
    )
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
        title="T",
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
        title="T",
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


async def test_on_message_callback_persists_incoming_and_is_idempotent(session: AsyncSession):
    gateway = FakeChatGateway()
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    msg = MessageInfo(
        message_id=501,
        chat_id=100,
        sender_id=200,
        text="Обычное сообщение покупателя",
        order_id="ord-1",
    )

    await callbacks.on_message(msg)  # type: ignore
    await callbacks.on_message(msg)  # type: ignore

    conversations = (await session.execute(select(ChatConversation))).scalars().all()
    messages = (await session.execute(select(ChatMessage))).scalars().all()
    assert len(conversations) == 1
    assert conversations[0].unread_count == 1
    assert conversations[0].buyer_funpay_id == "200"
    assert len(messages) == 1
    assert messages[0].delivery_status == "received"


async def test_on_message_never_dispatches_without_durable_idempotency_record(
    session: AsyncSession,
):
    gateway = FakeChatGateway()
    message = MessageInfo(
        message_id=777,
        chat_id=100,
        sender_id=200,
        text="!помощь",
        order_id=None,
    )

    with patch(
        "app.services.funpay_lifecycle.ChatService.record_event",
        new=AsyncMock(side_effect=RuntimeError("storage unavailable")),
    ):
        callbacks = build_callbacks(
            session_factory=lambda: session,
            gateway=gateway,
        )
        await callbacks.on_message(message)  # type: ignore[arg-type]

    assert gateway.sent_messages == []
    assert (await session.execute(select(ChatMessage))).scalars().all() == []


async def test_on_message_callback_stores_from_me_without_dispatch(session: AsyncSession):
    gateway = FakeChatGateway()
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    msg = MessageInfo(
        message_id=502,
        chat_id=100,
        sender_id=999,
        text="!помощь",
        order_id=None,
        from_me=True,
    )

    await callbacks.on_message(msg)  # type: ignore

    conversation = (await session.execute(select(ChatConversation))).scalar_one()
    stored = (await session.execute(select(ChatMessage))).scalar_one()
    assert conversation.unread_count == 0
    assert conversation.last_message_direction == "outgoing"
    assert stored.direction == "outgoing"
    assert stored.delivery_status == "sent"
    assert stored.is_read is True
    assert gateway.sent_messages == []


async def test_on_new_sale_creates_rental_when_account_available(session: AsyncSession):
    """Полный поток: new_sale → Order → Rental → welcome message."""
    from datetime import datetime, timedelta, timezone

    from app.models.account import Account, AccountLimits
    from app.models.rental import Rental
    from app.models.settings import SellerSettings
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    tier = SubscriptionTier(
        code="plus",
        name="Plus",
        is_active=True,
        is_sellable=True,
    )
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    session.add(SellerSettings(id=1, default_max_active_rentals=1))
    await session.flush()
    acc = Account(
        login="acc1",
        password_encrypted="pass",
        totp_secret_encrypted="totp",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    session.add(AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="enc",
        chat_5h_remaining_pct=80,
        chat_weekly_remaining_pct=70,
        codex_5h_remaining_pct=60,
        codex_weekly_remaining_pct=50,
        measured_at=datetime.now(timezone.utc),
        refresh_status="ok",
        plan_type="plus",
        plan_window_status="ok",
        expected_long_window_seconds=7 * 24 * 60 * 60,
    ))
    session.add(Lot(
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="T",
        title_en="T",
        status="active",
        auto_created=True,
    ))
    await session.flush()

    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="ord-99",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="T",
        price=599.0,
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    await callbacks.on_new_sale("ord-99")  # type: ignore

    rentals = (await session.execute(select(Rental))).scalars().all()
    assert len(rentals) == 1
    assert rentals[0].account_id == acc.id
    assert len(gateway.sent_messages) >= 1

    await callbacks.on_message(MessageInfo(
        message_id=1001,
        chat_id=100,
        sender_id=200,
        text="!help",
        order_id="ord-99",
    ))  # type: ignore[arg-type]
    order = (
        await session.execute(
            select(Order).where(Order.funpay_order_id == "ord-99")
        )
    ).scalar_one()
    persisted_rental = (
        await session.execute(select(Rental).where(Rental.order_id == order.id))
    ).scalar_one()
    assert order.buyer_locale == "en"
    assert persisted_rental.lang == "en"
    assert "Commands" in gateway.sent_messages[-1][1]
