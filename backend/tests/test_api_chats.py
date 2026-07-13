import pytest
from datetime import datetime, timezone
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.api.routers.chats import get_online_chat_gateway
from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import MessageInfo
from app.main import app
from app.models.chat import ChatConversation
from app.models.funpay_sale import FunPaySale
from app.models.lot import Lot
from app.models.rental import Order
from app.services.chat_service import ChatService, UnverifiedConversationError


_MANAGED_OFFER_ID = "9001"
_MANAGED_PROVENANCE_TOKEN = "abcdef0123456789abcdef0123456789"


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(COOKIE_NAME, token)
        yield client


async def _ensure_managed_lot(session: AsyncSession) -> Lot:
    lot = await session.scalar(
        select(Lot).where(Lot.funpay_id == _MANAGED_OFFER_ID)
    )
    if lot is not None:
        return lot
    lot = Lot(
        funpay_id=_MANAGED_OFFER_ID,
        provenance_token=_MANAGED_PROVENANCE_TOKEN,
        provenance_marker_synced=True,
        funpay_node_id=55,
        tier_id=1,
        duration_id=1,
        limit_scope_id=1,
        price=100,
        title_ru="T",
        title_en="T",
        status="active",
        auto_created=True,
    )
    session.add(lot)
    await session.flush()
    return lot


async def _seed_conversation(session: AsyncSession, *, message_id: int = 101) -> int:
    await _seed_sale(session)
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


async def _seed_sale(
    session: AsyncSession,
    *,
    chat_id: int = 500,
    buyer_id: int = 700,
    order_id: str = "order-500",
) -> FunPaySale:
    lot = await _ensure_managed_lot(session)
    local_order = Order(
        funpay_order_id=order_id,
        funpay_chat_id=str(chat_id),
        buyer_funpay_id=str(buyer_id),
        buyer_locale="ru",
        lot_id=lot.id,
        lot_binding_method="offer_id",
        funpay_offer_id=lot.funpay_id,
        price=100,
        status="pending",
    )
    session.add(local_order)
    await session.flush()
    sale = FunPaySale(
        funpay_order_id=order_id,
        order_id=local_order.id,
        funpay_chat_id=str(chat_id),
        buyer_funpay_id=str(buyer_id),
        buyer_username="verified-buyer",
        buyer_avatar_url="https://example.test/avatar.png",
        buyer_is_online=True,
        buyer_status_text="online",
        status="paid",
        created_at=datetime.now(timezone.utc),
        profile_checked_at=datetime.now(timezone.utc),
    )
    session.add(sale)
    await session.flush()
    return sale


async def test_list_chats_and_history(auth_client: AsyncClient, session: AsyncSession):
    conversation_id = await _seed_conversation(session)

    response = await auth_client.get("/api/chats")
    assert response.status_code == 200
    assert response.json()[0]["id"] == conversation_id
    assert response.json()[0]["unread_count"] == 1
    assert response.json()[0]["buyer_username"] == "verified-buyer"
    assert response.json()[0]["buyer_is_online"] is True
    assert response.json()[0]["sale_orders"][0]["funpay_order_id"] == "order-500"

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


async def test_login_codes_are_never_persisted_in_local_chat_history(
    session: AsyncSession,
):
    conversation_id = await _seed_conversation(session)
    service = ChatService()
    conversation = await service.get_conversation(session, conversation_id)
    plaintext = (
        "TOTP (приложение): 123456\n"
        "Email OTP OpenAI: 654321"
    )
    pending = await service.create_outgoing_pending(
        session, conversation, plaintext,
    )
    await session.commit()

    echoed, created = await service.record_event(
        session,
        MessageInfo(
            message_id=910,
            chat_id=500,
            sender_id=1,
            text=plaintext,
            order_id=None,
            from_me=True,
        ),
    )
    await session.commit()

    history = await service.list_messages(session, conversation_id)
    assert created is False
    assert echoed.id == pending.id
    assert "123456" not in pending.text
    assert "654321" not in pending.text
    assert pending.text.count("[скрыто]") == 2
    assert all("123456" not in item.text for item in history)
    assert all("654321" not in item.text for item in history)


async def test_chat_text_is_encrypted_at_rest_but_available_to_admin(
    session: AsyncSession,
):
    secret_message = "Логин buyer@example.com; пароль SuperSecret-123"
    await _seed_sale(
        session,
        chat_id=501,
        buyer_id=700,
        order_id="order-501",
    )
    stored, _ = await ChatService().record_event(
        session,
        MessageInfo(
            message_id=911,
            chat_id=501,
            sender_id=1,
            text=secret_message,
            order_id=None,
            from_me=True,
        ),
    )
    await session.commit()

    raw_message = (
        await session.execute(
            text("SELECT text FROM chat_messages WHERE id=:id"),
            {"id": stored.id},
        )
    ).scalar_one()
    raw_preview = (
        await session.execute(
            text(
                "SELECT last_message_text FROM chat_conversations "
                "WHERE id=:id"
            ),
            {"id": stored.conversation_id},
        )
    ).scalar_one()

    assert secret_message not in raw_message
    assert secret_message not in raw_preview
    assert stored.text == secret_message


async def test_unverified_purchase_chat_is_not_persisted(session: AsyncSession):
    with pytest.raises(UnverifiedConversationError):
        await ChatService().record_event(
            session,
            MessageInfo(
                message_id=999,
                chat_id=999,
                sender_id=123,
                text="I sold something to you",
                order_id="purchase-only",
            ),
        )
    assert (await session.execute(text("SELECT COUNT(*) FROM chat_messages"))).scalar_one() == 0


async def test_stale_verified_flag_without_sale_is_not_exposed(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    stale = ChatConversation(
        funpay_chat_id="777",
        buyer_funpay_id="888",
        verified_sale=True,
    )
    session.add(stale)
    await session.commit()

    response = await auth_client.get("/api/chats")
    assert response.status_code == 200
    assert response.json() == []
    response = await auth_client.get(f"/api/chats/{stale.id}/messages")
    assert response.status_code == 404


async def test_sale_for_order_without_bot_lot_cannot_expose_or_send_chat(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    foreign_order = Order(
        funpay_order_id="foreign-order",
        funpay_chat_id="778",
        buyer_funpay_id="889",
        buyer_locale="ru",
        lot_id=None,
        price=100,
        status="pending",
    )
    session.add(foreign_order)
    await session.flush()
    session.add(FunPaySale(
        funpay_order_id="foreign-order",
        order_id=foreign_order.id,
        funpay_chat_id="778",
        buyer_funpay_id="889",
        status="paid",
    ))
    foreign_chat = ChatConversation(
        funpay_chat_id="778",
        buyer_funpay_id="889",
        verified_sale=True,
    )
    session.add(foreign_chat)
    await session.commit()

    gateway = FakeChatGateway()
    app.dependency_overrides[get_online_chat_gateway] = lambda: gateway
    try:
        listing = await auth_client.get("/api/chats")
        history = await auth_client.get(
            f"/api/chats/{foreign_chat.id}/messages"
        )
        reply = await auth_client.post(
            f"/api/chats/{foreign_chat.id}/messages",
            json={"text": "This must never be sent"},
        )
    finally:
        app.dependency_overrides.pop(get_online_chat_gateway, None)

    assert listing.status_code == 200
    assert listing.json() == []
    assert history.status_code == 404
    assert reply.status_code == 404
    assert gateway.sent_messages == []
