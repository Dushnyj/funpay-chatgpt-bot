from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.runner import RunnerCallbacks
from app.integrations.funpay.types import MessageInfo, OrderInfo, SaleStatus
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.chat import ChatConversation, ChatMessage
from app.models.funpay_sale import FunPaySale
from app.models.lot import Lot
from app.models.rental import Order, Rental
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


async def _seed_active_rental(
    session: AsyncSession,
    *,
    order_id: str,
    chat_id: int,
    buyer_id: int,
    login: str,
    totp_secret: str,
) -> Rental:
    await _seed_verified_sale(
        session,
        order_id=order_id,
        chat_id=chat_id,
        buyer_id=buyer_id,
    )
    order = await session.scalar(
        select(Order).where(Order.funpay_order_id == order_id)
    )
    lot = await session.get(Lot, order.lot_id)
    account = Account(
        login=login,
        password_encrypted="enc",
        totp_secret_encrypted=totp_secret,
        tier_id=lot.tier_id,
        status="active",
        subscription_expires_at=(
            datetime.now(timezone.utc) + timedelta(days=30)
        ),
    )
    session.add(account)
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="enc",
        measured_at=datetime.now(timezone.utc),
        refresh_status="ok",
        plan_window_status="ok",
    ))
    rental = Rental(
        order_id=order.id,
        account_id=account.id,
        buyer_funpay_id=str(buyer_id),
        buyer_funpay_chat_id=str(chat_id),
        tier_id=lot.tier_id,
        duration_id=lot.duration_id,
        limit_scope_id=lot.limit_scope_id,
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
        credentials_delivery_status="sent",
        credentials_delivery_template="welcome",
        credentials_delivery_attempts=1,
        credentials_delivered_at=datetime.now(timezone.utc),
    )
    session.add(rental)
    await session.flush()
    return rental


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


async def test_telegram_failure_does_not_block_new_sale_fulfillment(
    session: AsyncSession,
):
    await _seed_lot(session)
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="telegram-down",
        status=SaleStatus.PAID,
        chat_id=101,
        buyer_id=201,
        subcategory_id=55,
        title="T",
        price=599.0,
        offer_id=int(_MANAGED_OFFER_ID),
    ))
    notifier = AsyncMock()
    call_order: list[str] = []

    async def fail_telegram(*_args):
        call_order.append("telegram")
        raise RuntimeError("telegram unavailable")

    async def fulfill_first(*_args, **_kwargs):
        call_order.append("fulfill")
        return None

    notifier.notify_new_order.side_effect = fail_telegram

    with (
        patch(
            "app.services.funpay_lifecycle.TelegramNotifier.from_settings",
            new=AsyncMock(return_value=notifier),
        ),
        patch(
            "app.services.funpay_lifecycle.RentalService.fulfill_order",
            new=AsyncMock(side_effect=fulfill_first),
        ) as fulfill,
    ):
        callbacks = build_callbacks(
            session_factory=lambda: session,
            gateway=gateway,
        )
        await callbacks.on_new_sale("telegram-down")  # type: ignore[misc]

    fulfill.assert_awaited_once()
    assert call_order == ["fulfill", "telegram"]


@pytest.mark.parametrize(
    ("remote_status", "expected_local_status"),
    [
        (SaleStatus.REFUNDED, "refunded"),
        (SaleStatus.UNKNOWN, "pending"),
    ],
)
async def test_non_fulfillable_new_sale_never_discloses_credentials(
    session: AsyncSession,
    remote_status: SaleStatus,
    expected_local_status: str,
):
    await _seed_lot(session)
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id=f"intake-{remote_status.value}",
        status=remote_status,
        chat_id=102,
        buyer_id=202,
        subcategory_id=55,
        title="T",
        price=599.0,
        offer_id=int(_MANAGED_OFFER_ID),
    ))

    with patch(
        "app.services.funpay_lifecycle.RentalService.fulfill_order",
        new=AsyncMock(return_value=None),
    ) as fulfill:
        callbacks = build_callbacks(
            session_factory=lambda: session,
            gateway=gateway,
        )
        await callbacks.on_new_sale(  # type: ignore[misc]
            f"intake-{remote_status.value}",
        )

    order = await session.scalar(
        select(Order).where(
            Order.funpay_order_id == f"intake-{remote_status.value}"
        )
    )
    assert order is not None
    assert order.status == expected_local_status
    fulfill.assert_not_awaited()


async def test_delayed_paid_new_sale_does_not_resurrect_refunded_order(
    session: AsyncSession,
):
    await _seed_verified_sale(
        session,
        order_id="delayed-paid-after-refund",
        chat_id=103,
        buyer_id=203,
    )
    order = await session.scalar(select(Order).where(
        Order.funpay_order_id == "delayed-paid-after-refund",
    ))
    sale = await session.scalar(select(FunPaySale).where(
        FunPaySale.funpay_order_id == "delayed-paid-after-refund",
    ))
    assert order is not None and sale is not None
    order.status = "refunded"
    sale.status = SaleStatus.REFUNDED.value
    await session.commit()
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id=order.funpay_order_id,
        status=SaleStatus.PAID,
        chat_id=103,
        buyer_id=203,
        subcategory_id=55,
        title="T",
        price=599.0,
        offer_id=int(_MANAGED_OFFER_ID),
    ))

    with patch(
        "app.services.funpay_lifecycle.RentalService.fulfill_order",
        new=AsyncMock(return_value=None),
    ) as fulfill:
        callbacks = build_callbacks(
            session_factory=lambda: session,
            gateway=gateway,
        )
        await callbacks.on_new_sale(order.funpay_order_id)  # type: ignore[misc]

    persisted_order = await session.scalar(select(Order).where(
        Order.funpay_order_id == "delayed-paid-after-refund",
    ))
    persisted_sale = await session.scalar(select(FunPaySale).where(
        FunPaySale.funpay_order_id == "delayed-paid-after-refund",
    ))
    assert persisted_order is not None and persisted_order.status == "refunded"
    assert persisted_sale is not None
    assert persisted_sale.status == SaleStatus.REFUNDED.value
    fulfill.assert_not_awaited()
    assert gateway.sent_messages == []


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


async def test_sale_close_notifies_exact_buyer_once(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    await _seed_lot(session)
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="close-once",
        status=SaleStatus.PAID,
        chat_id=321,
        buyer_id=654,
        subcategory_id=55,
        title="T",
        price=599.0,
        offer_id=int(_MANAGED_OFFER_ID),
    ))

    with patch(
        "app.services.funpay_lifecycle.RentalService.fulfill_order",
        new=AsyncMock(return_value=None),
    ):
        callbacks = build_callbacks(
            session_factory=lambda: session,
            gateway=gateway,
        )
        await callbacks.on_new_sale("close-once")  # type: ignore[misc]
        gateway.sent_messages.clear()
        await callbacks.on_sale_closed("close-once")  # type: ignore[misc]
        await callbacks.on_sale_closed("close-once")  # type: ignore[misc]

    assert len(gateway.sent_messages) == 1
    assert gateway.sent_messages[0][0] == 321


async def test_completed_sale_at_intake_notifies_exact_buyer(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    await _seed_lot(session)
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="completed-at-intake",
        status=SaleStatus.COMPLETED,
        chat_id=322,
        buyer_id=655,
        subcategory_id=55,
        title="T",
        price=599.0,
        offer_id=int(_MANAGED_OFFER_ID),
    ))

    with patch(
        "app.services.funpay_lifecycle.RentalService.fulfill_order",
        new=AsyncMock(return_value=None),
    ):
        callbacks = build_callbacks(
            session_factory=lambda: session,
            gateway=gateway,
        )
        await callbacks.on_new_sale("completed-at-intake")  # type: ignore[misc]

    assert len(gateway.sent_messages) == 1
    assert gateway.sent_messages[0][0] == 322


async def test_failed_buyer_confirmation_retries_on_duplicate_close(
    session: AsyncSession,
):
    from app.models.audit import AuditLog
    from app.services.order_notifications import BUYER_ORDER_CONFIRMED_EVENT
    from app.services.seed_data import seed_message_templates

    class FailFirstConfirmationGateway(FakeChatGateway):
        def __init__(self) -> None:
            super().__init__()
            self.confirmation_attempts = 0

        async def send_message(self, chat_id: int, text: str) -> int:
            self.confirmation_attempts += 1
            if self.confirmation_attempts == 1:
                raise RuntimeError("temporary FunPay send failure")
            return await super().send_message(chat_id, text)

    await seed_message_templates(session)
    await _seed_lot(session)
    gateway = FailFirstConfirmationGateway()
    gateway.set_order(OrderInfo(
        order_id="close-retry",
        status=SaleStatus.PAID,
        chat_id=323,
        buyer_id=656,
        subcategory_id=55,
        title="T",
        price=599.0,
        offer_id=int(_MANAGED_OFFER_ID),
    ))

    with patch(
        "app.services.funpay_lifecycle.RentalService.fulfill_order",
        new=AsyncMock(return_value=None),
    ):
        callbacks = build_callbacks(
            session_factory=lambda: session,
            gateway=gateway,
        )
        await callbacks.on_new_sale("close-retry")  # type: ignore[misc]
        await callbacks.on_sale_closed("close-retry")  # type: ignore[misc]
        assert await session.scalar(select(AuditLog.id).where(
            AuditLog.event_type == BUYER_ORDER_CONFIRMED_EVENT,
        )) is None

        order = await session.scalar(
            select(Order).where(Order.funpay_order_id == "close-retry")
        )
        assert order is not None
        assert order.confirmation_delivery_status == "failed"
        assert order.confirmation_delivery_attempts == 1
        assert order.confirmation_delivery_next_attempt_at is not None

        # A duplicate callback must respect the per-order retry backoff instead
        # of hammering the same broken FunPay chat immediately.
        await callbacks.on_sale_closed("close-retry")  # type: ignore[misc]
        assert gateway.confirmation_attempts == 1

        order = await session.scalar(
            select(Order).where(Order.funpay_order_id == "close-retry")
        )
        assert order is not None
        order.confirmation_delivery_next_attempt_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        await session.commit()
        await callbacks.on_sale_closed("close-retry")  # type: ignore[misc]

    assert gateway.confirmation_attempts == 2
    assert len(gateway.sent_messages) == 1
    assert gateway.sent_messages[0][0] == 323
    assert await session.scalar(select(AuditLog.id).where(
        AuditLog.event_type == BUYER_ORDER_CONFIRMED_EVENT,
    )) is not None


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


async def test_generic_chat_order_reference_routes_to_exact_active_rental(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    first = await _seed_active_rental(
        session,
        order_id="HHHGNZ4N",
        chat_id=909,
        buyer_id=200,
        login="first@example.test",
        totp_secret="FIRSTSECRETTOTP",
    )
    second = await _seed_active_rental(
        session,
        order_id="XQGVAQ85",
        chat_id=909,
        buyer_id=200,
        login="second@example.test",
        totp_secret="SECONDSECRETTOTP",
    )
    first_rental_id = first.id
    second_rental_id = second.id
    first_order_id = first.order_id
    second_order_id = second.order_id
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="XQGVAQ85",
        status=SaleStatus.PAID,
        chat_id=909,
        buyer_id=200,
        subcategory_id=55,
        title="ChatGPT",
        price=100,
        offer_id=5001,
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    message = MessageInfo(
        message_id=503,
        chat_id=909,
        sender_id=200,
        text="!code #xqgvaq85",
        order_id=None,
    )

    with patch(
        "app.services.command_handlers.generate_totp",
        return_value="654321",
    ) as generate:
        await callbacks.on_message(message)  # type: ignore[misc]

    generate.assert_called_once_with("SECONDSECRETTOTP")
    assert len(gateway.sent_messages) == 1
    assert "654321" in gateway.sent_messages[0][1]
    first = await session.get(Rental, first_rental_id)
    second = await session.get(Rental, second_rental_id)
    first_order = await session.get(Order, first_order_id)
    second_order = await session.get(Order, second_order_id)
    assert first.lang == "ru"
    assert first_order.buyer_locale == "ru"
    assert second.lang == "en"
    assert second_order.buyer_locale == "en"


@pytest.mark.parametrize(
    "text",
    ["!code #FOREIGN1", "!code XQGVAQ85", "!code #TOO-SHRT"],
)
async def test_generic_chat_foreign_or_malformed_reference_discloses_nothing(
    session: AsyncSession,
    text: str,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_active_rental(
        session,
        order_id="XQGVAQ85",
        chat_id=910,
        buyer_id=200,
        login="owned@example.test",
        totp_secret="OWNEDSECRETTOTP",
    )
    rental_id = rental.id
    order_id = rental.order_id
    gateway = FakeChatGateway()
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    message = MessageInfo(
        message_id=504,
        chat_id=910,
        sender_id=200,
        text=text,
        order_id=None,
    )

    with patch("app.services.command_handlers.generate_totp") as generate:
        await callbacks.on_message(message)  # type: ignore[misc]

    generate.assert_not_called()
    assert len(gateway.sent_messages) == 1
    assert "OWNEDSECRETTOTP" not in gateway.sent_messages[0][1]
    assert "XQGVAQ85" not in gateway.sent_messages[0][1]
    assert "NO ACTIVE ACCESS FOUND" in gateway.sent_messages[0][1]
    rental = await session.get(Rental, rental_id)
    order = await session.get(Order, order_id)
    assert rental.lang == "ru"
    assert order.buyer_locale == "ru"


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
        subscription_expiry_source="accounts_check",
    )
    session.add(acc)
    await session.flush()
    session.add(AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="enc",
        codex_5h_remaining_pct=60,
        codex_weekly_remaining_pct=50,
        codex_primary_remaining_pct=50,
        codex_primary_window_seconds=7 * 24 * 60 * 60,
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
    assert "BUYER COMMANDS" in gateway.sent_messages[-1][1]
