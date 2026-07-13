from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.catalog import Duration, LimitScope, SubscriptionTier


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_list_tiers_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/tiers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_tier_is_rejected(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.post("/api/tiers", json={"name": "Plus", "is_active": True})
    assert resp.status_code == 405


async def test_list_tier_returns_system_metadata(auth_client: AsyncClient, session: AsyncSession):
    session.add(SubscriptionTier(
        name="Pro 5x",
        code="pro_5x",
        system_managed=True,
        is_sellable=False,
        sort_order=40,
        usage_multiplier=5.0,
    ))
    await session.commit()
    resp = await auth_client.get("/api/tiers")
    assert resp.status_code == 200
    assert resp.json()[0]["code"] == "pro_5x"
    assert resp.json()[0]["usage_multiplier"] == 5.0


async def test_update_tier(auth_client: AsyncClient, session: AsyncSession):
    tier = SubscriptionTier(name="Plus", code="plus", is_sellable=False)
    session.add(tier)
    await session.commit()
    resp = await auth_client.patch(
        f"/api/tiers/{tier.id}",
        json={"is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    assert resp.json()["is_sellable"] is False

    resp = await auth_client.patch(
        f"/api/tiers/{tier.id}",
        json={"is_sellable": True},
    )
    assert resp.status_code == 422


async def test_delete_tier(auth_client: AsyncClient, session: AsyncSession):
    tier = SubscriptionTier(name="Plus", code="plus")
    session.add(tier)
    await session.commit()
    resp = await auth_client.delete(f"/api/tiers/{tier.id}")
    assert resp.status_code == 405


async def test_list_durations(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/durations")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_update_duration_availability_and_order(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    lifecycle = type("Lifecycle", (), {"reconcile_lots": AsyncMock(return_value=[])})()
    monkeypatch.setattr(app.state, "lifecycle", lifecycle, raising=False)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    await session.commit()

    resp = await auth_client.patch(
        f"/api/durations/{duration.id}",
        json={"is_enabled": False, "sort_order": 25},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "id": duration.id,
        "days": 7,
        "is_enabled": False,
        "sort_order": 25,
    }
    lifecycle.reconcile_lots.assert_awaited_once_with()


async def test_update_duration_sort_order_only_skips_reconciliation(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    lifecycle = type("Lifecycle", (), {"reconcile_lots": AsyncMock(return_value=[])})()
    monkeypatch.setattr(app.state, "lifecycle", lifecycle, raising=False)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    await session.commit()

    resp = await auth_client.patch(
        f"/api/durations/{duration.id}",
        json={"sort_order": 25},
    )

    assert resp.status_code == 200
    assert resp.json()["sort_order"] == 25
    lifecycle.reconcile_lots.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"is_enabled": None},
        {"days": 14, "is_enabled": False},
        {"sort_order": -1},
    ],
)
async def test_update_duration_rejects_unsafe_payloads(
    auth_client: AsyncClient,
    session: AsyncSession,
    payload: dict,
):
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    await session.commit()

    resp = await auth_client.patch(f"/api/durations/{duration.id}", json=payload)

    assert resp.status_code == 422
    await session.refresh(duration)
    assert duration.days == 7
    assert duration.is_enabled is True


async def test_update_durations_batch_rejects_unknown_id_atomically(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    await session.commit()

    resp = await auth_client.patch(
        "/api/durations/batch",
        json=[
            {"id": duration.id, "is_enabled": False},
            {"id": duration.id + 1000, "is_enabled": False},
        ],
    )

    assert resp.status_code == 404
    await session.refresh(duration)
    assert duration.is_enabled is True


async def test_update_durations_batch_sort_only_skips_reconciliation(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    lifecycle = type("Lifecycle", (), {"reconcile_lots": AsyncMock(return_value=[])})()
    monkeypatch.setattr(app.state, "lifecycle", lifecycle, raising=False)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    await session.commit()

    resp = await auth_client.patch(
        "/api/durations/batch",
        json=[{"id": duration.id, "sort_order": 25}],
    )

    assert resp.status_code == 200
    assert resp.json()[0]["sort_order"] == 25
    lifecycle.reconcile_lots.assert_not_awaited()


async def test_update_limit_scope(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    lifecycle = type("Lifecycle", (), {"reconcile_lots": AsyncMock(return_value=[])})()
    monkeypatch.setattr(app.state, "lifecycle", lifecycle, raising=False)
    scope = LimitScope(
        code="codex",
        name="Codex",
        is_enabled=True,
        sort_order=30,
    )
    session.add(scope)
    await session.commit()

    resp = await auth_client.patch(
        f"/api/limit-scopes/{scope.id}",
        json={"is_enabled": False, "sort_order": 15},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "id": scope.id,
        "code": "codex",
        "name": "Codex",
        "is_enabled": False,
        "sort_order": 15,
    }
    lifecycle.reconcile_lots.assert_awaited_once_with()


async def test_update_limit_scope_sort_order_only_skips_reconciliation(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    lifecycle = type("Lifecycle", (), {"reconcile_lots": AsyncMock(return_value=[])})()
    monkeypatch.setattr(app.state, "lifecycle", lifecycle, raising=False)
    scope = LimitScope(
        code="codex",
        name="Codex",
        is_enabled=True,
        sort_order=30,
    )
    session.add(scope)
    await session.commit()

    resp = await auth_client.patch(
        f"/api/limit-scopes/{scope.id}",
        json={"sort_order": 15},
    )

    assert resp.status_code == 200
    assert resp.json()["sort_order"] == 15
    lifecycle.reconcile_lots.assert_not_awaited()


async def test_unchanged_availability_skips_reconciliation(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    lifecycle = type("Lifecycle", (), {"reconcile_lots": AsyncMock(return_value=[])})()
    monkeypatch.setattr(app.state, "lifecycle", lifecycle, raising=False)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    scope = LimitScope(
        code="codex",
        name="Codex",
        is_enabled=True,
        sort_order=30,
    )
    session.add_all([duration, scope])
    await session.commit()

    duration_response = await auth_client.patch(
        f"/api/durations/{duration.id}",
        json={"is_enabled": True},
    )
    batch_response = await auth_client.patch(
        "/api/durations/batch",
        json=[{"id": duration.id, "is_enabled": True}],
    )
    scope_response = await auth_client.patch(
        f"/api/limit-scopes/{scope.id}",
        json={"is_enabled": True},
    )

    assert duration_response.status_code == 200
    assert batch_response.status_code == 200
    assert scope_response.status_code == 200
    lifecycle.reconcile_lots.assert_not_awaited()


async def test_update_limit_scope_rejects_enabling_unmeasurable_chat(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    scope = LimitScope(
        code="chat",
        name="Chat",
        is_enabled=False,
        sort_order=20,
    )
    session.add(scope)
    await session.commit()

    resp = await auth_client.patch(
        f"/api/limit-scopes/{scope.id}",
        json={"is_enabled": True},
    )

    assert resp.status_code == 422
    await session.refresh(scope)
    assert scope.is_enabled is False


async def test_update_limit_scope_rejects_enabling_unknown_legacy_scope(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    scope = LimitScope(
        code="legacy",
        name="Legacy",
        is_enabled=False,
        sort_order=100,
    )
    session.add(scope)
    await session.commit()

    resp = await auth_client.patch(
        f"/api/limit-scopes/{scope.id}",
        json={"is_enabled": True},
    )

    assert resp.status_code == 422
    await session.refresh(scope)
    assert scope.is_enabled is False


@pytest.mark.parametrize(
    "payload",
    [{}, {"is_enabled": None}, {"code": "other", "is_enabled": False}],
)
async def test_update_limit_scope_rejects_unsafe_payloads(
    auth_client: AsyncClient,
    session: AsyncSession,
    payload: dict,
):
    scope = LimitScope(code="any", name="Any", is_enabled=True, sort_order=10)
    session.add(scope)
    await session.commit()

    resp = await auth_client.patch(f"/api/limit-scopes/{scope.id}", json=payload)

    assert resp.status_code == 422
    await session.refresh(scope)
    assert scope.code == "any"
    assert scope.is_enabled is True


async def test_unauthorized_request_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/tiers")
        assert resp.status_code == 401
