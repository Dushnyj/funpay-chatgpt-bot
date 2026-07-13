from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.account import Account
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import Lot, PriceMatrix
from app.models.rental import Order, Rental


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


async def test_list_durations_sorted_strictly_by_days(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    session.add_all([
        Duration(days=8, is_enabled=True, sort_order=1),
        Duration(days=3, is_enabled=True, sort_order=999),
        Duration(days=5, is_enabled=True, sort_order=0),
    ])
    await session.commit()

    resp = await auth_client.get("/api/durations")

    assert resp.status_code == 200
    assert [item["days"] for item in resp.json()] == [3, 5, 8]


async def test_create_custom_duration_uses_days_as_internal_sort_order(
    auth_client: AsyncClient,
):
    resp = await auth_client.post("/api/durations", json={"days": 8})

    assert resp.status_code == 201
    assert resp.json() == {
        "id": resp.json()["id"],
        "days": 8,
        "is_enabled": True,
        "sort_order": 8,
    }


async def test_create_custom_duration_accepts_boundary_days_and_options(
    auth_client: AsyncClient,
):
    first = await auth_client.post(
        "/api/durations",
        json={"days": 1, "is_enabled": False},
    )
    last = await auth_client.post("/api/durations", json={"days": 30})

    assert first.status_code == 201
    assert first.json()["days"] == 1
    assert first.json()["is_enabled"] is False
    assert first.json()["sort_order"] == 1
    assert last.status_code == 201
    assert last.json()["days"] == 30


async def test_create_custom_duration_duplicate_returns_conflict(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    session.add(Duration(days=8, is_enabled=False, sort_order=80))
    await session.commit()

    resp = await auth_client.post(
        "/api/durations",
        json={"days": 8, "is_enabled": True},
    )

    assert resp.status_code == 409
    durations = (
        await session.execute(select(Duration).where(Duration.days == 8))
    ).scalars().all()
    assert len(durations) == 1
    assert durations[0].is_enabled is False
    assert durations[0].sort_order == 80


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"days": 0},
        {"days": -1},
        {"days": 31},
        {"days": 8.0},
        {"days": "8"},
        {"days": True},
        {"days": 8, "sort_order": 10},
        {"days": 8, "unknown": "field"},
    ],
)
async def test_create_custom_duration_rejects_invalid_payload(
    auth_client: AsyncClient,
    payload: dict,
):
    resp = await auth_client.post("/api/durations", json=payload)

    assert resp.status_code == 422


async def test_update_duration_availability(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    lifecycle = type("Lifecycle", (), {"reconcile_lots": AsyncMock(return_value=[])})()
    monkeypatch.setattr(app.state, "lifecycle", lifecycle, raising=False)
    duration = Duration(days=7, is_enabled=True, sort_order=7)
    session.add(duration)
    await session.commit()

    resp = await auth_client.patch(
        f"/api/durations/{duration.id}",
        json={"is_enabled": False},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "id": duration.id,
        "days": 7,
        "is_enabled": False,
        "sort_order": 7,
    }
    lifecycle.reconcile_lots.assert_awaited_once_with()


async def test_update_duration_rejects_sort_order(
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

    assert resp.status_code == 422
    lifecycle.reconcile_lots.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"is_enabled": None},
        {"days": 14, "is_enabled": False},
        {"sort_order": 25},
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


async def test_update_durations_batch_rejects_sort_order(
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

    assert resp.status_code == 422
    lifecycle.reconcile_lots.assert_not_awaited()


async def test_delete_unused_duration(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    duration = Duration(days=8, is_enabled=True, sort_order=8)
    session.add(duration)
    await session.commit()

    response = await auth_client.delete(f"/api/durations/{duration.id}")

    assert response.status_code == 204
    assert await session.get(Duration, duration.id) is None


async def test_delete_duration_reports_every_reference_without_cascade(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    tier = SubscriptionTier(
        code="plus",
        name="Plus",
        is_active=True,
        is_sellable=True,
    )
    duration = Duration(days=8, is_enabled=True, sort_order=8)
    scope = LimitScope(code="any", name="Any", is_enabled=True, sort_order=10)
    account = Account(
        login="duration-delete@example.com",
        password_encrypted="password",
        totp_secret_encrypted="totp",
        tier_id=None,
        status="active",
    )
    session.add_all([tier, duration, scope, account])
    await session.flush()
    matrix = PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    )
    lot = Lot(
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Лот",
        title_en="Lot",
        status="paused",
        auto_created=False,
    )
    session.add_all([matrix, lot])
    await session.flush()
    order = Order(
        funpay_order_id="duration-delete-order",
        funpay_chat_id="chat",
        buyer_funpay_id="buyer",
        lot_id=lot.id,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    )
    session.add(order)
    await session.flush()
    now = datetime.now(timezone.utc)
    rental = Rental(
        order_id=order.id,
        account_id=account.id,
        buyer_funpay_id="buyer",
        buyer_funpay_chat_id="chat",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        started_at=now,
        expires_at=now + timedelta(days=8),
    )
    session.add(rental)
    await session.commit()

    response = await auth_client.delete(f"/api/durations/{duration.id}")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "price_matrix=1" in detail
    assert "lots=1" in detail
    assert "orders=1" in detail
    assert "rentals=1" in detail
    assert await session.get(Duration, duration.id) is not None
    assert await session.get(PriceMatrix, matrix.id) is not None
    assert await session.get(Lot, lot.id) is not None
    assert await session.get(Order, order.id) is not None
    assert await session.get(Rental, rental.id) is not None


async def test_delete_duration_handles_concurrent_reference_race(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    duration = Duration(days=8, is_enabled=True, sort_order=8)
    session.add(duration)
    await session.commit()
    commit = AsyncMock(
        side_effect=IntegrityError(
            "DELETE FROM durations",
            {},
            RuntimeError("foreign key violation"),
        )
    )
    rollback = AsyncMock(wraps=session.rollback)
    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(session, "rollback", rollback)

    response = await auth_client.delete(f"/api/durations/{duration.id}")

    assert response.status_code == 409
    assert "became referenced" in response.json()["detail"]
    commit.assert_awaited_once_with()
    rollback.assert_awaited_once_with()


async def test_delete_duration_not_found(auth_client: AsyncClient):
    response = await auth_client.delete("/api/durations/999999")

    assert response.status_code == 404


async def test_limit_scopes_use_fixed_canonical_order(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    session.add_all([
        LimitScope(code="codex", name="Codex", sort_order=1),
        LimitScope(code="any", name="Any", sort_order=999),
        LimitScope(code="chat", name="Chat", sort_order=0),
        LimitScope(code="legacy", name="Legacy", sort_order=-100),
    ])
    await session.commit()

    response = await auth_client.get("/api/limit-scopes")

    assert response.status_code == 200
    assert [item["code"] for item in response.json()] == [
        "any",
        "chat",
        "codex",
        "legacy",
    ]


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
        json={"is_enabled": False},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "id": scope.id,
        "code": "codex",
        "name": "Codex",
        "is_enabled": False,
        "sort_order": 30,
    }
    lifecycle.reconcile_lots.assert_awaited_once_with()


async def test_update_limit_scope_rejects_sort_order(
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

    assert resp.status_code == 422
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
    [
        {},
        {"is_enabled": None},
        {"code": "other", "is_enabled": False},
        {"sort_order": 5},
    ],
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
