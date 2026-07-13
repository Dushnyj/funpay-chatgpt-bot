import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.runner import RunnerCallbacks
from app.integrations.funpay.types import MessageInfo, OrderInfo, SaleStatus
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.chat import ChatConversation, ChatMessage
from app.models.funpay_sale import FunPaySale
from app.models.lot import Lot
from app.models.rental import Order
from app.services.funpay_lifecycle import build_callbacks
from app.services.sale_registry import SaleRegistryService


_MANAGED_OFFER_ID = "9001"
_MANAGED_PROVENANCE_TOKEN = "0123456789abcdef0123456789abcdef"


async def _seed_verified_sale(
    session: AsyncSession,
    *,
    order_id: str = "ord-1",
    chat_id: int = 100,
    buyer_id: int = 200,
) -> None:
    lot_id = await _seed_lot(session)
    order = Order(
        funpay_order_id=order_id,
        funpay_chat_id=str(chat_id),
        buyer_funpay_id=str(buyer_id),
        buyer_locale="ru",
        lot_id=lot_id,
        lot_binding_method="offer_id",
        funpay_offer_id=_MANAGED_OFFER_ID,
        price=100,
        status="pending",
    )
    session.add(order)
    await session.flush()
    session.add(FunPaySale(
        funpay_order_id=order_id,
        order_id=order.id,
        funpay_chat_id=str(chat_id),
        buyer_funpay_id=str(buyer_id),
        status="paid",
    ))
    await session.flush()


async def _seed_lot(session: AsyncSession) -> int:
    existing = await session.scalar(
        select(Lot).where(Lot.funpay_id == _MANAGED_OFFER_ID)
    )
    if existing is not None:
        return existing.id

    tier = SubscriptionTier(
        code="plus", name="Plus", is_active=True, is_sellable=True,
    )
    session.add(tier)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    lot = Lot(
        funpay_id=_MANAGED_OFFER_ID,
        provenance_token=_MANAGED_PROVENANCE_TOKEN,
        provenance_marker_synced=True,
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
        offer_id=int(_MANAGED_OFFER_ID),
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    await callbacks.on_new_sale("ord-1")  # type: ignore
    result = await session.execute(select(Order).where(Order.funpay_order_id == "ord-1"))
    assert result.scalar_one_or_none() is not None


async def test_new_sale_continues_after_concurrent_registry_insert(
    session: AsyncSession,
):
    """A preview-sync winner must not make the event lose fulfillment."""

    await _seed_lot(session)
    await _seed_verified_sale(
        session,
        order_id="race-sale",
        chat_id=710,
        buyer_id=810,
    )
    await session.commit()

    class StaleFirstReadRegistry(SaleRegistryService):
        def __init__(self) -> None:
            self.lookup_count = 0

        async def _get_by_remote_order(self, db, order_id):
            self.lookup_count += 1
            if self.lookup_count == 1:
                # Deterministically model a SELECT that raced just before the
                # periodic sync committed its unique-key winner.
                return None
            return await super()._get_by_remote_order(db, order_id)

    registry = StaleFirstReadRegistry()
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="race-sale",
        status=SaleStatus.PAID,
        chat_id=710,
        buyer_id=810,
        subcategory_id=55,
        title="T",
        price=599.0,
        offer_id=int(_MANAGED_OFFER_ID),
    ))
    callbacks = build_callbacks(
        session_factory=lambda: session,
        gateway=gateway,
        sale_registry=registry,
    )

    await callbacks.on_new_sale("race-sale")  # type: ignore[misc]

    sales = list((await session.execute(
        select(FunPaySale).where(FunPaySale.funpay_order_id == "race-sale")
    )).scalars())
    order = await session.scalar(
        select(Order).where(Order.funpay_order_id == "race-sale")
    )
    assert registry.lookup_count >= 2
    assert len(sales) == 1
    assert order is not None
    assert sales[0].order_id == order.id


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
        offer_id=int(_MANAGED_OFFER_ID),
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    await callbacks.on_new_sale("ord-1")  # type: ignore
    await callbacks.on_sale_closed("ord-1")  # type: ignore
    order = await session.get(Order, 1)
    assert order.status == "completed"


async def test_unmatched_sale_never_creates_provenance_or_chat(
    session: AsyncSession,
):
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="unmatched-sale",
        status=SaleStatus.PAID,
        chat_id=301,
        buyer_id=401,
        subcategory_id=999999,
        title="Unknown lot",
        price=123.0,
        buyer_username="real-buyer",
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)

    await callbacks.on_new_sale("unmatched-sale")  # type: ignore
    sale = await session.scalar(
        select(FunPaySale).where(FunPaySale.funpay_order_id == "unmatched-sale")
    )
    assert sale is None
    assert await session.scalar(
        select(Order).where(Order.funpay_order_id == "unmatched-sale")
    ) is None
    assert await session.scalar(select(ChatConversation)) is None

    await callbacks.on_sale_closed("unmatched-sale")  # type: ignore
    await callbacks.on_sale_refunded("unmatched-sale")  # type: ignore
    assert await session.scalar(select(FunPaySale)) is None
    assert await session.scalar(select(ChatConversation)) is None


async def test_on_message_callback_dispatches_command(session: AsyncSession):
    await _seed_verified_sale(session)
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
    await _seed_verified_sale(session)
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
    await _seed_verified_sale(session)
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
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
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
        codex_5h_remaining_pct=60,
        codex_weekly_remaining_pct=50,
        measured_at=datetime.now(timezone.utc),
        refresh_status="ok",
        plan_type="plus",
        plan_window_status="ok",
        expected_long_window_seconds=7 * 24 * 60 * 60,
    ))
    session.add(Lot(
        funpay_id=_MANAGED_OFFER_ID,
        provenance_token=_MANAGED_PROVENANCE_TOKEN,
        provenance_marker_synced=True,
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
        offer_id=int(_MANAGED_OFFER_ID),
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
