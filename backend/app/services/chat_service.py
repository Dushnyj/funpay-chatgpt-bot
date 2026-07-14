from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import re

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.types import MessageInfo
from app.models.chat import ChatConversation, ChatMessage
from app.models.funpay_sale import FunPaySale
from app.services.order_provenance import managed_sale_order_exists
from app.services.sale_registry import SaleRegistryService


class ConversationNotFoundError(LookupError):
    pass


class UnverifiedConversationError(LookupError):
    """Message did not come from a buyer of an exactly bound bot lot."""


@dataclass(frozen=True, slots=True)
class SaleOrderSummary:
    order_id: int | None
    funpay_order_id: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatConversationSummary:
    id: int
    funpay_chat_id: str
    buyer_funpay_id: str
    buyer_username: str | None
    buyer_avatar_url: str | None
    buyer_is_online: bool | None
    buyer_status_text: str | None
    profile_checked_at: datetime | None
    funpay_order_id: str | None
    order_id: int | None
    unread_count: int
    last_message_text: str | None
    last_message_direction: str | None
    last_message_at: datetime | None
    sale_orders: tuple[SaleOrderSummary, ...]



_LOGIN_CODE_RE = re.compile(
    r"(?i)(\b(?:TOTP(?:\s*\([^)]*\))?|Email OTP OpenAI|OpenAI email OTP|"
    r"Код подтверждения|Authenticator code|Код из письма OpenAI|"
    r"OpenAI email code)"
    r"\s*:\s*)\d{6}\b"
)
_SIX_DIGIT_CODE_RE = re.compile(r"\b\d{6}\b")


def _redact_login_codes(text: str) -> str:
    """Prevent buyer TOTP/email OTP values from entering local chat history."""
    redacted = _LOGIN_CODE_RE.sub(r"\1[скрыто]", text)
    if redacted == text:
        return text
    # ``email_code_success`` is operator-editable, so its surrounding label is
    # not trustworthy. A !code response always contains the required labelled
    # TOTP value; once that marker is detected, redact every other six-digit
    # token in the same response as a potential email OTP as well.
    return _SIX_DIGIT_CODE_RE.sub("[скрыто]", redacted)


class ChatService:
    """Persistence operations for the admin FunPay inbox.

    Transaction boundaries intentionally stay with the callback/API caller. This
    lets the incoming callback commit the message before command processing and
    lets the send endpoint persist a pending outbox row before network I/O.
    """

    def __init__(self, sale_registry: SaleRegistryService | None = None) -> None:
        self._sales = sale_registry or SaleRegistryService()

    async def record_event(
        self,
        session: AsyncSession,
        message: MessageInfo,
    ) -> tuple[ChatMessage, bool]:
        source_id = str(message.message_id)
        conversation = await self._find_or_create_conversation(session, message)
        stored_text = _redact_login_codes(message.text or "") if message.from_me else (
            message.text or ""
        )
        existing = await session.scalar(
            select(ChatMessage).where(
                ChatMessage.conversation_id == conversation.id,
                ChatMessage.funpay_message_id == source_id,
            )
        )
        if existing is not None:
            return existing, False

        # FunPay can echo an admin reply before the HTTP send endpoint has
        # stored the returned message ID. Merge that echo into the pending
        # outbox row instead of creating a duplicate outgoing bubble.
        if message.from_me:
            pending_candidates = list(
                (
                    await session.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == conversation.id,
                    ChatMessage.direction == "outgoing",
                    ChatMessage.delivery_status == "pending",
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                        .limit(20)
                    )
                ).scalars()
            )
            # Fernet is randomized, so encrypted text cannot be compared in
            # SQL. Decrypt only a bounded pending outbox window and compare in
            # memory instead.
            pending = next(
                (item for item in pending_candidates if item.text == stored_text),
                None,
            )
            if pending is not None:
                pending.funpay_message_id = source_id
                pending.delivery_status = "sent"
                pending.is_read = True
                await session.flush()
                return pending, False

        created_at = datetime.now(timezone.utc)
        direction = "outgoing" if message.from_me else "incoming"
        stored = ChatMessage(
            conversation_id=conversation.id,
            funpay_message_id=source_id,
            direction=direction,
            sender_funpay_id=str(message.sender_id) if message.sender_id is not None else None,
            text=stored_text,
            delivery_status="sent" if message.from_me else "received",
            is_read=message.from_me,
            created_at=created_at,
        )
        session.add(stored)
        if not message.from_me:
            conversation.unread_count += 1
        self._set_last_message(conversation, stored.text, direction, created_at)
        await session.flush()
        return stored, True

    async def list_conversations(
        self, session: AsyncSession
    ) -> list[ChatConversationSummary]:
        result = await session.execute(
            select(ChatConversation)
            .where(ChatConversation.verified_sale.is_(True))
        )
        conversations = list(result.scalars().all())
        if not conversations:
            return []
        buyer_ids = {
            item.buyer_funpay_id
            for item in conversations
            if item.buyer_funpay_id is not None
        }
        chat_ids = {item.funpay_chat_id for item in conversations}
        sales = list(
            (
                await session.execute(
                    select(FunPaySale)
                    .where(
                        managed_sale_order_exists(allow_pending_chat=True),
                        (FunPaySale.buyer_funpay_id.in_(buyer_ids))
                        | (FunPaySale.funpay_chat_id.in_(chat_ids))
                    )
                    .order_by(FunPaySale.created_at.desc(), FunPaySale.id.desc())
                )
            ).scalars()
        )
        sales_by_buyer: dict[str, list[FunPaySale]] = {}
        exact_sale_keys: set[tuple[str, str]] = set()
        for sale in sales:
            sales_by_buyer.setdefault(sale.buyer_funpay_id, []).append(sale)
            if sale.funpay_chat_id is not None:
                exact_sale_keys.add((sale.buyer_funpay_id, sale.funpay_chat_id))

        sortable: list[tuple[datetime, ChatConversationSummary]] = []
        for conversation in conversations:
            if conversation.buyer_funpay_id is None or (
                conversation.buyer_funpay_id,
                conversation.funpay_chat_id,
            ) not in exact_sale_keys:
                # A stale/corrupt boolean alone cannot expose chat history.
                continue
            buyer_sales = sales_by_buyer.get(conversation.buyer_funpay_id, [])
            summary = ChatConversationSummary(
                id=conversation.id,
                funpay_chat_id=conversation.funpay_chat_id,
                buyer_funpay_id=conversation.buyer_funpay_id,
                buyer_username=conversation.buyer_username,
                buyer_avatar_url=conversation.buyer_avatar_url,
                buyer_is_online=conversation.buyer_is_online,
                buyer_status_text=conversation.buyer_status_text,
                profile_checked_at=conversation.profile_checked_at,
                funpay_order_id=conversation.funpay_order_id,
                order_id=conversation.order_id,
                unread_count=conversation.unread_count,
                last_message_text=conversation.last_message_text,
                last_message_direction=conversation.last_message_direction,
                last_message_at=conversation.last_message_at,
                sale_orders=tuple(
                    SaleOrderSummary(
                        order_id=sale.order_id,
                        funpay_order_id=sale.funpay_order_id,
                        status=sale.status,
                        created_at=sale.created_at,
                    )
                    for sale in buyer_sales
                ),
            )
            newest_sale_at = buyer_sales[0].created_at
            if newest_sale_at.tzinfo is None:
                newest_sale_at = newest_sale_at.replace(tzinfo=timezone.utc)
            last_message_at = conversation.last_message_at
            if last_message_at is not None and last_message_at.tzinfo is None:
                last_message_at = last_message_at.replace(tzinfo=timezone.utc)
            activity_at = (
                max(last_message_at, newest_sale_at)
                if last_message_at is not None
                else newest_sale_at
            )
            sortable.append((activity_at, summary))
        sortable.sort(key=lambda item: (item[0], item[1].id), reverse=True)
        return [item[1] for item in sortable]

    async def get_conversation(
        self,
        session: AsyncSession,
        conversation_id: int,
        *,
        lock_route: bool = False,
    ) -> ChatConversation:
        matching_sale = (
            select(FunPaySale.id)
            .where(
                managed_sale_order_exists(),
                FunPaySale.funpay_chat_id == ChatConversation.funpay_chat_id,
                FunPaySale.buyer_funpay_id == ChatConversation.buyer_funpay_id,
            )
            .exists()
        )
        statement = select(ChatConversation).where(
                ChatConversation.id == conversation_id,
                ChatConversation.verified_sale.is_(True),
                matching_sale,
            )
        if lock_route:
            statement = statement.with_for_update()
        conversation = await session.scalar(statement)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    async def list_messages(
        self,
        session: AsyncSession,
        conversation_id: int,
        *,
        limit: int = 200,
    ) -> list[ChatMessage]:
        await self.get_conversation(session, conversation_id)
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit)
        )
        messages = list(result.scalars().all())
        messages.reverse()
        return messages

    async def mark_read(self, session: AsyncSession, conversation_id: int) -> ChatConversation:
        conversation = await self.get_conversation(session, conversation_id)
        await session.execute(
            update(ChatMessage)
            .where(
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.direction == "incoming",
                ChatMessage.is_read.is_(False),
            )
            .values(is_read=True)
        )
        conversation.unread_count = 0
        await session.flush()
        return conversation

    async def create_outgoing_pending(
        self,
        session: AsyncSession,
        conversation: ChatConversation,
        text: str,
    ) -> ChatMessage:
        created_at = datetime.now(timezone.utc)
        stored_text = _redact_login_codes(text)
        message = ChatMessage(
            conversation_id=conversation.id,
            direction="outgoing",
            text=stored_text,
            delivery_status="pending",
            is_read=True,
            created_at=created_at,
        )
        session.add(message)
        self._set_last_message(conversation, stored_text, "outgoing", created_at)
        await session.flush()
        return message

    async def mark_outgoing_sent(
        self,
        session: AsyncSession,
        message: ChatMessage,
        funpay_message_id: int,
    ) -> None:
        message.delivery_status = "sent"
        if funpay_message_id > 0:
            message.funpay_message_id = str(funpay_message_id)
        await session.flush()

    async def mark_outgoing_failed(
        self,
        session: AsyncSession,
        message: ChatMessage,
    ) -> None:
        message.delivery_status = "failed"
        await session.flush()

    async def _find_or_create_conversation(
        self,
        session: AsyncSession,
        message: MessageInfo,
    ) -> ChatConversation:
        if message.chat_id <= 0:
            raise UnverifiedConversationError(message.chat_id)
        sale = await self._sales.resolve_message_sale(session, message)
        if sale is None:
            raise UnverifiedConversationError(message.chat_id)
        conversation = await self._sales.ensure_conversation(
            session, sale, message.chat_id
        )
        if conversation is None or not conversation.verified_sale:
            raise UnverifiedConversationError(message.chat_id)
        if (
            not message.from_me
            and message.sender_username
            and message.sender_id is not None
            and str(message.sender_id) == sale.buyer_funpay_id
        ):
            sale.buyer_username = message.sender_username
            conversation.buyer_username = message.sender_username
        return conversation

    @staticmethod
    def _set_last_message(
        conversation: ChatConversation,
        text: str,
        direction: str,
        created_at: datetime,
    ) -> None:
        conversation.last_message_text = text
        conversation.last_message_direction = direction
        conversation.last_message_at = created_at
        conversation.updated_at = created_at
