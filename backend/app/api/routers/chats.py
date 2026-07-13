from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat_schemas import (
    ChatMessageOut,
    ChatSummaryOut,
    MarkChatReadResponse,
    SendChatMessageRequest,
)
from app.api.deps import get_current_user, get_db_session
from app.integrations.funpay.gateway import ChatGateway
from app.services.chat_service import ChatService, ConversationNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/chats",
    tags=["chats"],
    dependencies=[Depends(get_current_user)],
)
service = ChatService()
_ADMIN_REPLY_TIMEOUT_SECONDS = 30.0


def get_online_chat_gateway(request: Request) -> ChatGateway:
    """Resolve the live FunPay transport without owning its lifecycle.

    The runtime lifecycle remains the sole owner of the runner/gateway. The API
    only reads its state and fails explicitly when no live transport exists.
    """

    lifecycle = getattr(request.app.state, "lifecycle", None)
    runner = getattr(lifecycle, "runner", None)
    if runner is None or not getattr(runner, "started", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FunPay bot is offline; reply cannot be delivered",
        )
    try:
        gateway = runner.gateway
    except Exception:
        logger.exception("Live FunPay runner has no available gateway")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FunPay bot is offline; reply cannot be delivered",
        )
    return gateway


@router.get("", response_model=list[ChatSummaryOut])
async def list_chats(session: AsyncSession = Depends(get_db_session)):
    return await service.list_conversations(session)


@router.get("/{conversation_id}/messages", response_model=list[ChatMessageOut])
async def list_chat_messages(
    conversation_id: int,
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await service.list_messages(session, conversation_id, limit=limit)
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Chat not found")


@router.post("/{conversation_id}/read", response_model=MarkChatReadResponse)
async def mark_chat_read(
    conversation_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        conversation = await service.mark_read(session, conversation_id)
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Chat not found")
    await session.commit()
    return MarkChatReadResponse(status="ok", unread_count=conversation.unread_count)


@router.post(
    "/{conversation_id}/messages",
    response_model=ChatMessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def send_chat_message(
    conversation_id: int,
    req: SendChatMessageRequest,
    session: AsyncSession = Depends(get_db_session),
    gateway: ChatGateway = Depends(get_online_chat_gateway),
):
    try:
        conversation = await service.get_conversation(session, conversation_id)
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Chat not found")

    try:
        chat_id = int(conversation.funpay_chat_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="Chat has an invalid FunPay identifier")

    pending = await service.create_outgoing_pending(session, conversation, req.text)
    await session.commit()

    # The outbox commit releases the initial authorization snapshot. Re-check
    # the exact sale/order/lot chain before external I/O so a concurrent
    # quarantine or chat rebind cannot send to the stale node.
    try:
        authorized = await service.get_conversation(
            session,
            conversation_id,
            lock_route=True,
        )
    except ConversationNotFoundError:
        await service.mark_outgoing_failed(session, pending)
        await session.commit()
        raise HTTPException(status_code=409, detail="Chat authorization changed")
    if authorized.funpay_chat_id != str(chat_id):
        await service.mark_outgoing_failed(session, pending)
        await session.commit()
        raise HTTPException(status_code=409, detail="Chat identifier changed")

    try:
        funpay_message_id = await asyncio.wait_for(
            gateway.send_message(chat_id=chat_id, text=req.text),
            timeout=_ADMIN_REPLY_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("Failed to deliver admin reply to FunPay chat %s", chat_id)
        await service.mark_outgoing_failed(session, pending)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FunPay bot is unavailable; reply was not delivered",
        )

    await service.mark_outgoing_sent(session, pending, funpay_message_id)
    await session.commit()
    await session.refresh(pending)
    return pending
