"""One realistic, fully local acceptance flow for a managed FunPay sale.

The test deliberately crosses the same callback boundaries as production:
sale intake, durable provenance, fulfillment, chat command handling, sale
completion, and scheduled expiry.  No external service is contacted.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import MessageInfo, OrderInfo, SaleStatus
from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.chat import ChatConversation, ChatMessage
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.funpay_sale import FunPaySale
from app.models.lot import Lot
from app.models.rental import Order, Rental
from app.services.command_handlers import CodeHandler
from app.services.command_parser import CommandType
from app.services.command_router import CommandRouter
from app.services.funpay_lifecycle import build_callbacks
from app.services.kick_service import KickResult
from app.services.rental_expiry import RentalExpiryService
from app.services.seed_data import seed_message_templates


class RecordingKickService:
    def __init__(self) -> None:
        self.account_ids: list[int] = []

    async def kick(self, _session: AsyncSession, account_id: int) -> KickResult:
        self.account_ids.append(account_id)
        return KickResult(success=True)


async def test_managed_paid_sale_from_delivery_to_expiry(test_engine) -> None:
    """Exercise the buyer-visible happy path and its idempotency boundaries."""

    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    now = datetime.now(timezone.utc)
    provenance_token = "a" * 32
    order_id = "SYSTEM-FLOW-1"
    chat_id = 700_001
    buyer_id = 800_002
    account_password = "Rental-Pass-123!"

    async with session_factory() as session:
        await seed_message_templates(session, commit=False)
        tier = SubscriptionTier(
            code="plus",
            name="Plus",
            is_active=True,
            is_sellable=True,
            system_managed=True,
        )
        duration = Duration(minutes=30, is_enabled=True, sort_order=30)
        scope = LimitScope(
            code="any",
            name="Без гарантии лимита",
            is_enabled=True,
            sort_order=10,
        )
        session.add_all((tier, duration, scope))
        await session.flush()

        lot = Lot(
            funpay_id="424242",
            provenance_token=provenance_token,
            provenance_marker_synced=True,
            funpay_node_id=55,
            tier_id=tier.id,
            duration_id=duration.id,
            limit_scope_id=scope.id,
            price=599,
            title_ru="ChatGPT Plus на 30 минут",
            title_en="ChatGPT Plus for 30 minutes",
            status="active",
            auto_created=True,
        )
        account = Account(
            login="system-flow@example.test",
            password_encrypted=account_password,
            totp_secret_encrypted="JBSWY3DPEHPK3PXP",
            tier_id=tier.id,
            status="active",
            subscription_expires_at=now + timedelta(days=30),
            subscription_expiry_source="accounts_check",
        )
        session.add_all((lot, account))
        await session.flush()
        account_id = account.id
        lot_id = lot.id
        session.add(
            AccountLimits(
                account_id=account.id,
                refresh_token_encrypted="local-refresh-token",
                codex_primary_remaining_pct=87,
                codex_primary_window_seconds=7 * 24 * 60 * 60,
                codex_primary_resets_at=now + timedelta(days=6),
                plan_type="plus",
                plan_window_status="ok",
                expected_long_window_seconds=7 * 24 * 60 * 60,
                measured_at=now,
                refresh_status="ok",
            )
        )
        await session.commit()

    gateway = FakeChatGateway()
    gateway.set_order(
        OrderInfo(
            order_id=order_id,
            status=SaleStatus.PAID,
            chat_id=chat_id,
            buyer_id=buyer_id,
            subcategory_id=55,
            # Display fields intentionally do not match the local lot. Only
            # the immutable, published marker is allowed to authorize it.
            title="Untrusted display title",
            price=1.0,
            full_description=(
                "FunPay order snapshot\n\n"
                f"[FPBOT:{provenance_token}]"
            ),
            buyer_username="verified-buyer",
        )
    )

    router = CommandRouter()
    callbacks = build_callbacks(session_factory, gateway, command_router=router)
    # Avoid a real-time wait for the next long-lived TOTP window while keeping
    # all authorization and disclosure logic identical to production.
    router.register(
        CommandType.CODE,
        CodeHandler(totp_min_validity_s=0),
    )

    assert callbacks.on_new_sale is not None
    await callbacks.on_new_sale(order_id)

    async with session_factory() as session:
        order = await session.scalar(
            select(Order).where(Order.funpay_order_id == order_id)
        )
        assert order is not None
        assert order.lot_id == lot_id
        assert order.lot_binding_method == "provenance_token"
        assert order.funpay_offer_id is None
        assert order.lot_provenance_token == provenance_token

        sale = await session.scalar(
            select(FunPaySale).where(FunPaySale.funpay_order_id == order_id)
        )
        assert sale is not None
        assert sale.order_id == order.id
        assert sale.funpay_chat_id == str(chat_id)
        assert sale.buyer_funpay_id == str(buyer_id)

        rental = await session.scalar(
            select(Rental).where(Rental.order_id == order.id)
        )
        assert rental is not None
        rental_id = rental.id
        assert rental.account_id == account_id
        assert rental.credentials_delivery_status == "sent"
        assert rental.credentials_delivery_attempts == 1

        conversation = await session.scalar(
            select(ChatConversation).where(
                ChatConversation.funpay_chat_id == str(chat_id)
            )
        )
        assert conversation is not None
        assert conversation.verified_sale is True
        assert conversation.buyer_funpay_id == str(buyer_id)

    assert len(gateway.sent_messages) == 1
    welcome_chat_id, welcome_text = gateway.sent_messages[0]
    assert welcome_chat_id == chat_id
    assert "system-flow@example.test" in welcome_text
    assert account_password in welcome_text

    # Replayed NewSale must reuse both the Order and Rental and must never
    # disclose the credentials a second time.
    await callbacks.on_new_sale(order_id)
    assert len(gateway.sent_messages) == 1
    async with session_factory() as session:
        assert len(list((await session.execute(select(Order))).scalars())) == 1
        assert len(list((await session.execute(select(FunPaySale))).scalars())) == 1
        assert len(list((await session.execute(select(Rental))).scalars())) == 1

    # Closing the sale has one buyer-facing acknowledgement; a duplicate
    # callback is a no-op for both state and messaging.
    assert callbacks.on_sale_closed is not None
    await callbacks.on_sale_closed(order_id)
    assert len(gateway.sent_messages) == 2
    await callbacks.on_sale_closed(order_id)
    assert len(gateway.sent_messages) == 2
    async with session_factory() as session:
        order = await session.scalar(
            select(Order).where(Order.funpay_order_id == order_id)
        )
        sale = await session.scalar(
            select(FunPaySale).where(FunPaySale.funpay_order_id == order_id)
        )
        assert order is not None and order.status == "completed"
        assert sale is not None and sale.status == "completed"

    # A command is authorized only for the exact buyer/chat/order tuple.
    assert callbacks.on_message is not None
    await callbacks.on_message(
        MessageInfo(
            message_id=9_001,
            chat_id=chat_id,
            sender_id=buyer_id,
            text="!код",
            order_id=order_id,
            buyer_id=buyer_id,
            from_me=False,
            sender_username="verified-buyer",
        )
    )
    assert len(gateway.sent_messages) == 3
    otp_chat_id, otp_text = gateway.sent_messages[-1]
    assert otp_chat_id == chat_id
    code_match = re.search(r"Код подтверждения: (\d{6})", otp_text)
    assert code_match is not None
    disclosed_code = code_match.group(1)

    # FunPay echoes the sent response back through the message callback. The
    # remote buyer receives the real OTP, while durable admin history stores
    # only a redacted representation.
    await callbacks.on_message(
        MessageInfo(
            message_id=9_002,
            chat_id=chat_id,
            sender_id=None,
            text=otp_text,
            order_id=order_id,
            buyer_id=buyer_id,
            from_me=True,
        )
    )
    async with session_factory() as session:
        stored_otp = await session.scalar(
            select(ChatMessage).where(
                ChatMessage.funpay_message_id == "9002"
            )
        )
        conversation = await session.scalar(
            select(ChatConversation).where(
                ChatConversation.funpay_chat_id == str(chat_id)
            )
        )
        assert stored_otp is not None
        assert stored_otp.direction == "outgoing"
        assert "Код подтверждения: [скрыто]" in stored_otp.text
        assert disclosed_code not in stored_otp.text
        assert conversation is not None
        assert disclosed_code not in (conversation.last_message_text or "")
        assert "[скрыто]" in (conversation.last_message_text or "")

        rental = await session.get(Rental, rental_id)
        assert rental is not None
        rental.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    kick = RecordingKickService()
    expiry_service = RentalExpiryService(kick_service=kick)
    async with session_factory() as session:
        expired = await expiry_service.expire_overdue(session, gateway)
        assert [item.id for item in expired] == [rental_id]

    assert kick.account_ids == [account_id]
    assert len(gateway.sent_messages) == 4
    expiry_chat_id, expiry_text = gateway.sent_messages[-1]
    assert expiry_chat_id == chat_id
    assert expiry_text

    async with session_factory() as session:
        rental = await session.get(Rental, rental_id)
        account = await session.get(Account, account_id)
        recovery_job = await session.scalar(
            select(AccountCheckJob).where(
                AccountCheckJob.account_id == account_id,
                AccountCheckJob.job_type == "refresh_recover",
            )
        )
        assert rental is not None
        assert rental.status == "expired"
        assert rental.expiry_notified_at is not None
        assert account is not None and account.status == "maintenance"
        assert recovery_job is not None

    # Scheduler re-entry is terminally idempotent: no second kick or expiry
    # notification is produced.
    async with session_factory() as session:
        assert await expiry_service.expire_overdue(session, gateway) == []
    assert kick.account_ids == [account_id]
    assert len(gateway.sent_messages) == 4
