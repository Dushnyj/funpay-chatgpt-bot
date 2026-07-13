import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot


class _Lifecycle:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.synced: list[tuple[int, bool]] = []

    async def sync_manual_lot(self, lot_id: int, active: bool = True):
        lot = await self.session.get(Lot, lot_id)
        lot.funpay_id = str(10_000 + lot_id)
        lot.status = "active" if active else "paused"
        lot.paused_reason = None if active else "manual"
        await self.session.commit()
        self.synced.append((lot_id, active))
        return int(lot.funpay_id)

    async def set_lot_active(self, lot_id: int, active: bool):
        lot = await self.session.get(Lot, lot_id)
        lot.status = "active" if active else "paused"
        lot.paused_reason = None if active else "manual"
        await self.session.commit()
        self.synced.append((lot_id, active))

    async def reconcile_lots(self):
        return []


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def _seed_catalog(session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    return tier, duration, scope


async def test_list_lots_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/lots")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_manual_lot(auth_client: AsyncClient, session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    tier.is_sellable = True
    lifecycle = _Lifecycle(session)
    app.state.lifecycle = lifecycle
    try:
        resp = await auth_client.post("/api/lots", json={
            "funpay_node_id": 55,
            "tier_id": tier.id, "duration_id": duration.id, "limit_scope_id": scope.id,
            "price": 599, "title_ru": "Тест", "title_en": "Test",
        })
    finally:
        del app.state.lifecycle
    assert resp.status_code == 201
    data = resp.json()
    assert data["price"] == 599
    assert data["auto_created"] is False
    assert data["status"] == "active"
    assert data["funpay_id"] is not None
    assert lifecycle.synced == [(data["id"], True)]


async def test_create_manual_lot_rejects_disabled_duration(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    tier.is_sellable = True
    duration.is_enabled = False
    await session.flush()

    response = await auth_client.post("/api/lots", json={
        "funpay_node_id": 55,
        "tier_id": tier.id,
        "duration_id": duration.id,
        "limit_scope_id": scope.id,
        "price": 599,
        "title_ru": "Тест",
        "title_en": "Test",
    })

    assert response.status_code == 422
    assert "duration is disabled" in response.json()["detail"]


async def test_delete_lot(auth_client: AsyncClient, session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    tier.is_sellable = True
    lifecycle = _Lifecycle(session)
    app.state.lifecycle = lifecycle
    try:
        resp = await auth_client.post("/api/lots", json={
            "funpay_node_id": 55,
            "tier_id": tier.id, "duration_id": duration.id, "limit_scope_id": scope.id,
            "price": 599, "title_ru": "Т", "title_en": "T",
        })
        lot_id = resp.json()["id"]
        resp = await auth_client.delete(f"/api/lots/{lot_id}")
    finally:
        del app.state.lifecycle
    assert resp.status_code == 204
    lot = await session.get(Lot, lot_id)
    assert lot.status == "deleted"
    assert lifecycle.synced[-1] == (lot_id, False)


async def test_pause_and_reactivate_lot_are_synchronized(
    auth_client: AsyncClient, session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    tier.is_sellable = True
    lifecycle = _Lifecycle(session)
    app.state.lifecycle = lifecycle
    try:
        created = await auth_client.post("/api/lots", json={
            "funpay_node_id": 55,
            "tier_id": tier.id,
            "duration_id": duration.id,
            "limit_scope_id": scope.id,
            "price": 599,
            "title_ru": "Т",
            "title_en": "T",
        })
        lot_id = created.json()["id"]
        paused = await auth_client.patch(
            f"/api/lots/{lot_id}", json={"status": "paused"}
        )
        active = await auth_client.patch(
            f"/api/lots/{lot_id}", json={"status": "active"}
        )
    finally:
        del app.state.lifecycle

    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    assert active.status_code == 200 and active.json()["status"] == "active"
    assert lifecycle.synced[-2:] == [(lot_id, False), (lot_id, True)]
