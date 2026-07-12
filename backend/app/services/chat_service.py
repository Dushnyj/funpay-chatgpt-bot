from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.types import MessageInfo
from app.models.chat import ChatConversation, ChatMessage
from app.models.rental import Order


class ConversationNotFoundError(LookupError):
    pass


class ChatService:
    """Persistence operations for the admin FunPay inbox.

    Transaction boundaries intentionally stay with the callback/API caller. This
    lets the incoming callback commit the message before command processing and
    lets the send endpoint persist a pending outbox row before network I/O.
    """

    async def record_event(
        self,
        session: AsyncSession,
        message: MessageInfo,
    ) -> tuple[ChatMessage, bool]:
        source_id = str(message.message_id)
        conversation = await self._find_or_create_conversation(session, message)
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
            pending = await session.scalar(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == conversation.id,
                    ChatMessage.direction == "outgoing",
                    ChatMessage.delivery_status == "pending",
                    ChatMessage.text == (message.text or ""),
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
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
            text=message.text or "",
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

    async def list_conversations(self, session: AsyncSession) -> list[ChatConversation]:
        result = await session.execute(
            select(ChatConversation).order_by(
                ChatConversation.last_message_at.desc(),
                ChatConversation.id.desc(),
            )
        )
        return list(result.scalars().all())

    async def get_conversation(
        self,
        session: AsyncSession,
        conversation_id: int,
    ) -> ChatConversation:
        conversation = await session.get(ChatConversation, conversation_id)
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
        message = ChatMessage(
            conversation_id=conversation.id,
            direction="outgoing",
            text=text,
            delivery_status="pending",
            is_read=True,
            created_at=created_at,
        )
        session.add(message)
        self._set_last_message(conversation, text, "outgoing", created_at)
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
        chat_id = str(message.chat_id)
        conversation = await session.scalar(
            select(ChatConversation).where(ChatConversation.funpay_chat_id == chat_id)
        )
        order = await self._find_order(session, message)

        if conversation is None:
            buyer_funpay_id = order.buyer_funpay_id if order is not None else None
            if buyer_funpay_id is None and message.sender_id is not None and not message.from_me:
                buyer_funpay_id = str(message.sender_id)
            conversation = ChatConversation(
                funpay_chat_id=chat_id,
                buyer_funpay_id=buyer_funpay_id,
                funpay_order_id=(
                    order.funpay_order_id if order is not None else message.order_id
                ),
                order_id=order.id if order is not None else None,
            )
            session.add(conversation)
            await session.flush()
            return conversation

        if order is not None:
            conversation.order_id = order.id
            conversation.funpay_order_id = order.funpay_order_id
            conversation.buyer_funpay_id = order.buyer_funpay_id
        elif (
            conversation.buyer_funpay_id is None
            and message.sender_id is not None
            and not message.from_me
        ):
            conversation.buyer_funpay_id = str(message.sender_id)
        if conversation.funpay_order_id is None and message.order_id:
            conversation.funpay_order_id = message.order_id
        return conversation

    @staticmethod
    async def _find_order(session: AsyncSession, message: MessageInfo) -> Order | None:
        if message.order_id:
            order = await session.scalar(
                select(Order).where(Order.funpay_order_id == message.order_id)
            )
            if order is not None:
                return order
        return await session.scalar(
            select(Order)
            .where(Order.funpay_chat_id == str(message.chat_id))
            .order_by(Order.id.desc())
        )

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
