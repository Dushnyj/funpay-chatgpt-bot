import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.settings import SellerSettings


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_get_settings(auth_client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1, funpay_node_id=55, default_max_active_rentals=3))
    await session.commit()
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["funpay_node_id"] == 55
    assert data["default_max_active_rentals"] == 3
    assert "admin_password_hash" not in data


async def test_update_settings(auth_client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1))
    await session.commit()
    resp = await auth_client.put("/api/settings", json={"default_max_active_rentals": 5})
    assert resp.status_code == 200
    assert resp.json()["default_max_active_rentals"] == 5


async def test_get_settings_not_configured(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 404


async def test_funpay_key_api_is_write_only_and_encrypted_at_rest(
    auth_client: AsyncClient, session: AsyncSession
):
    session.add(SellerSettings(id=1))
    await session.commit()
    secret = "0123456789abcdef0123456789abcdef"

    updated = await auth_client.put(
        "/api/settings/funpay-key", json={"key": secret}
    )
    status = await auth_client.get("/api/settings/funpay-key")

    assert updated.status_code == 200
    assert updated.json() == {"configured": True, "last4": "cdef"}
    assert status.json() == {"configured": True, "last4": "cdef"}
    assert secret not in updated.text
    assert secret not in status.text
    raw = (
        await session.execute(
            text("SELECT funpay_session_key FROM seller_settings WHERE id=1")
        )
    ).scalar_one()
    assert raw != secret
    session.expire_all()
    settings = await session.get(SellerSettings, 1)
    assert settings is not None and settings.funpay_session_key == secret


async def test_funpay_key_clear_does_not_return_secret(
    auth_client: AsyncClient, session: AsyncSession
):
    session.add(SellerSettings(id=1, funpay_session_key="stored-secret-1234"))
    await session.commit()

    response = await auth_client.delete("/api/settings/funpay-key")

    assert response.status_code == 200
    assert response.json() == {"configured": False, "last4": None}


async def test_funpay_key_changes_reconfigure_live_runner(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    session.add(SellerSettings(id=1))
    await session.commit()
    calls: list[str | None] = []

    class Lifecycle:
        async def reconfigure_funpay(self, key=None):
            calls.append(key)
            return bool(key)

    previous = getattr(app.state, "lifecycle", None)
    app.state.lifecycle = Lifecycle()
    try:
        secret = "0123456789abcdef0123456789abcdef"
        updated = await auth_client.put(
            "/api/settings/funpay-key", json={"key": secret}
        )
        cleared = await auth_client.delete("/api/settings/funpay-key")
    finally:
        if previous is None:
            del app.state.lifecycle
        else:
            app.state.lifecycle = previous

    assert updated.status_code == 200
    assert cleared.status_code == 200
    assert calls == [secret, None]


async def test_telegram_api_masks_token_and_encrypts_it(
    auth_client: AsyncClient, session: AsyncSession
):
    session.add(SellerSettings(id=1))
    await session.commit()
    token = "123456789:telegram-secret-token"

    updated = await auth_client.put(
        "/api/settings/telegram",
        json={"token": token, "seller_chat_id": "987654"},
    )
    changed_chat = await auth_client.put(
        "/api/settings/telegram", json={"seller_chat_id": "111222"}
    )

    assert updated.json() == {
        "configured": True,
        "token_last4": "oken",
        "seller_chat_id": "987654",
    }
    assert changed_chat.json()["seller_chat_id"] == "111222"
    assert changed_chat.json()["token_last4"] == "oken"
    assert token not in updated.text
    assert token not in changed_chat.text
    raw = (
        await session.execute(
            text("SELECT telegram_bot_token FROM seller_settings WHERE id=1")
        )
    ).scalar_one()
    assert raw != token
    session.expire_all()
    settings = await session.get(SellerSettings, 1)
    assert settings is not None and settings.telegram_bot_token == token

    cleared = await auth_client.delete("/api/settings/telegram")
    assert cleared.json() == {
        "configured": False,
        "token_last4": None,
        "seller_chat_id": None,
    }


async def test_telegram_test_endpoint_maps_failure_to_502(
    auth_client: AsyncClient, session: AsyncSession, monkeypatch
):
    session.add(
        SellerSettings(
            id=1,
            telegram_bot_token="123456789:test-token",
            telegram_seller_chat_id="123",
        )
    )
    await session.commit()

    async def fail(_self):
        raise RuntimeError("network error")

    monkeypatch.setattr("app.api.routers.settings.TelegramNotifier.send_test", fail)
    response = await auth_client.post("/api/settings/telegram/test")

    assert response.status_code == 502
    assert response.json() == {"detail": "Telegram test failed"}
