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
    session.add(SellerSettings(id=1, funpay_node_id=55, default_max_active_rentals=1))
    await session.commit()
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["funpay_node_id"] == 55
    assert data["default_max_active_rentals"] == 1
    assert data["graph_configured"] is False
    assert data["refresh_recover_concurrency"] == 3
    assert "admin_password_hash" not in data


async def test_get_settings_reports_graph_configuration_without_secrets(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    from app.config import get_settings as get_app_settings

    session.add(SellerSettings(id=1))
    await session.commit()
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "MICROSOFT_GRAPH_REDIRECT_URI",
        "https://example.test/api/email-oauth/microsoft/callback",
    )
    get_app_settings.cache_clear()

    response = await auth_client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["graph_configured"] is True
    assert "client-id" not in response.text
    assert "client-secret" not in response.text


async def test_update_settings(auth_client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1))
    await session.commit()
    resp = await auth_client.put(
        "/api/settings", json={"default_max_active_rentals": 5}
    )
    assert resp.status_code == 422


async def test_update_scheduler_settings_hot_reloads_lifecycle(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    from unittest.mock import AsyncMock

    session.add(SellerSettings(id=1))
    await session.commit()

    class Lifecycle:
        reload_settings = AsyncMock()

    previous = getattr(app.state, "lifecycle", None)
    app.state.lifecycle = Lifecycle()
    try:
        response = await auth_client.put(
            "/api/settings",
            json={
                "check_interval_minutes": 20,
                "refresh_recover_concurrency": 4,
                "refresh_max_attempts": 5,
                "refresh_retry_delay_minutes": 6,
                "check_delay_seconds": 30,
            },
        )
    finally:
        if previous is None:
            del app.state.lifecycle
        else:
            app.state.lifecycle = previous

    assert response.status_code == 200
    assert response.json()["refresh_recover_concurrency"] == 4
    Lifecycle.reload_settings.assert_awaited_once()


async def test_get_settings_not_configured(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 404


async def test_funpay_key_api_is_write_only_and_encrypted_at_rest(
    auth_client: AsyncClient, session: AsyncSession
):
    session.add(SellerSettings(id=1))
    await session.commit()
    secret = "0123456789abcdef0123456789abcdef"

    class Lifecycle:
        async def reconfigure_funpay(self, key=None):
            return bool(key)

    previous = getattr(app.state, "lifecycle", None)
    app.state.lifecycle = Lifecycle()
    try:
        updated = await auth_client.put(
            "/api/settings/funpay-key", json={"key": secret}
        )
        status = await auth_client.get("/api/settings/funpay-key")
    finally:
        if previous is None:
            del app.state.lifecycle
        else:
            app.state.lifecycle = previous

    assert updated.status_code == 200
    assert updated.json() == {
        "configured": True,
        "connected": True,
        "last4": "cdef",
    }
    assert status.json() == {
        "configured": True,
        "connected": True,
        "last4": "cdef",
    }
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
    assert response.json() == {
        "configured": False,
        "connected": False,
        "last4": None,
    }


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
    assert calls == [secret, ""]


async def test_funpay_key_clear_stops_old_runner_before_invalid_env_fallback(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    from app.config import get_settings

    fallback = "fallback-key-0123456789abcdef0123"
    monkeypatch.setenv("FUNPAY_SESSION_KEY", fallback)
    get_settings.cache_clear()
    session.add(SellerSettings(
        id=1,
        funpay_session_key="stored-key-0123456789abcdef012345",
        funpay_session_valid=True,
    ))
    await session.commit()
    calls: list[str | None] = []

    class Lifecycle:
        async def reconfigure_funpay(self, key=None):
            calls.append(key)
            return False

    previous = getattr(app.state, "lifecycle", None)
    app.state.lifecycle = Lifecycle()
    try:
        response = await auth_client.delete("/api/settings/funpay-key")
    finally:
        if previous is None:
            del app.state.lifecycle
        else:
            app.state.lifecycle = previous
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert calls == ["", fallback]


async def test_invalid_funpay_key_preserves_persisted_key_and_live_runner(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    old_key = "old-key-0123456789abcdef01234567"
    rejected_key = "bad-key-0123456789abcdef01234567"
    session.add(SellerSettings(
        id=1,
        funpay_session_key=old_key,
        funpay_session_valid=True,
    ))
    await session.commit()
    calls: list[str | None] = []

    class Lifecycle:
        async def reconfigure_funpay(self, key=None):
            calls.append(key)
            return False

    previous = getattr(app.state, "lifecycle", None)
    app.state.lifecycle = Lifecycle()
    try:
        response = await auth_client.put(
            "/api/settings/funpay-key",
            json={"key": rejected_key},
        )
    finally:
        if previous is None:
            del app.state.lifecycle
        else:
            app.state.lifecycle = previous

    assert response.status_code == 422
    assert rejected_key not in response.text
    assert calls == [rejected_key]
    session.expire_all()
    persisted = await session.get(SellerSettings, 1)
    assert persisted is not None
    assert persisted.funpay_session_key == old_key
    assert persisted.funpay_session_valid is True


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
