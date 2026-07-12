import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.api.routers.chats import get_online_chat_gateway
from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import MessageInfo
from app.main import app
from app.services.chat_service import ChatService


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(COOKIE_NAME, token)
        yield client


async def _seed_conversation(session: AsyncSession, *, message_id: int = 101) -> int:
    message, _ = await ChatService().record_event(
        session,
        MessageInfo(
            message_id=message_id,
            chat_id=500,
            sender_id=700,
            text="Здравствуйте, нужна помощь",
            order_id="order-500",
        ),
    )
    await session.commit()
    return message.conversation_id


async def test_list_chats_and_history(auth_client: AsyncClient, session: AsyncSession):
    conversation_id = await _seed_conversation(session)

    response = await auth_client.get("/api/chats")
    assert response.status_code == 200
    assert response.json()[0]["id"] == conversation_id
    assert response.json()[0]["unread_count"] == 1

    response = await auth_client.get(f"/api/chats/{conversation_id}/messages")
    assert response.status_code == 200
    assert response.json()[0]["text"] == "Здравствуйте, нужна помощь"
    assert response.json()[0]["direction"] == "incoming"


async def test_mark_chat_read(auth_client: AsyncClient, session: AsyncSession):
    conversation_id = await _seed_conversation(session)

    response = await auth_client.post(f"/api/chats/{conversation_id}/read")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "unread_count": 0}

    response = await auth_client.get("/api/chats")
    assert response.json()[0]["unread_count"] == 0


async def test_send_reply_uses_funpay_gateway(auth_client: AsyncClient, session: AsyncSession):
    conversation_id = await _seed_conversation(session)
    gateway = FakeChatGateway()
    app.dependency_overrides[get_online_chat_gateway] = lambda: gateway
    try:
        response = await auth_client.post(
            f"/api/chats/{conversation_id}/messages",
            json={"text": "Сейчас помогу"},
        )
    finally:
        app.dependency_overrides.pop(get_online_chat_gateway, None)

    assert response.status_code == 201
    assert response.json()["direction"] == "outgoing"
    assert response.json()["delivery_status"] == "sent"
    assert gateway.sent_messages == [(500, "Сейчас помогу")]


async def test_send_reply_returns_clear_503_when_bot_offline(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    conversation_id = await _seed_conversation(session)

    response = await auth_client.post(
        f"/api/chats/{conversation_id}/messages",
        json={"text": "Ответ"},
    )

    assert response.status_code == 503
    assert "offline" in response.json()["detail"]


async def test_gateway_failure_returns_503_and_persists_failed_message(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    class FailingGateway(FakeChatGateway):
        async def send_message(self, chat_id: int, text: str) -> int:
            raise RuntimeError("connection lost")

    conversation_id = await _seed_conversation(session)
    app.dependency_overrides[get_online_chat_gateway] = lambda: FailingGateway()
    try:
        response = await auth_client.post(
            f"/api/chats/{conversation_id}/messages",
            json={"text": "Ответ"},
        )
    finally:
        app.dependency_overrides.pop(get_online_chat_gateway, None)

    assert response.status_code == 503
    history = await ChatService().list_messages(session, conversation_id)
    assert history[-1].direction == "outgoing"
    assert history[-1].delivery_status == "failed"


async def test_blank_reply_is_rejected(auth_client: AsyncClient, session: AsyncSession):
    conversation_id = await _seed_conversation(session)
    gateway = FakeChatGateway()
    app.dependency_overrides[get_online_chat_gateway] = lambda: gateway
    try:
        response = await auth_client.post(
            f"/api/chats/{conversation_id}/messages",
            json={"text": "   "},
        )
    finally:
        app.dependency_overrides.pop(get_online_chat_gateway, None)

    assert response.status_code == 422
    assert gateway.sent_messages == []


async def test_from_me_echo_merges_with_pending_outbox(session: AsyncSession):
    conversation_id = await _seed_conversation(session)
    service = ChatService()
    conversation = await service.get_conversation(session, conversation_id)
    pending = await service.create_outgoing_pending(session, conversation, "Ответ продавца")
    await session.commit()

    echoed, created = await service.record_event(
        session,
        MessageInfo(
            message_id=909,
            chat_id=500,
            sender_id=1,
            text="Ответ продавца",
            order_id=None,
            from_me=True,
        ),
    )
    await session.commit()

    history = await service.list_messages(session, conversation_id)
    assert created is False
    assert echoed.id == pending.id
    assert echoed.funpay_message_id == "909"
    assert echoed.delivery_status == "sent"
    assert len(history) == 2
