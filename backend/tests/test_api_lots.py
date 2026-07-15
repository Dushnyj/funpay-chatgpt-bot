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
        self.deleted: list[int] = []

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

    async def delete_lot(self, lot_id: int):
        lot = await self.session.get(Lot, lot_id)
        lot.status = "deleted"
        lot.paused_reason = "manual_deleted"
        await self.session.commit()
        self.deleted.append(lot_id)


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def _seed_catalog(session: AsyncSession):
    tier = SubscriptionTier(code="plus", name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
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


async def test_create_manual_lot_rejects_disabled_limit_scope(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    tier.is_sellable = True
    scope.is_enabled = False
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
    assert "limit scope is disabled" in response.json()["detail"]


@pytest.mark.parametrize(
    ("unavailable_catalog", "expected_detail"),
    [
        ("tier_inactive", "tariff is unavailable"),
        ("tier_unsellable", "tariff is unavailable"),
        ("funpay_form", "not supported by the FunPay"),
        ("duration", "duration is disabled"),
        ("scope", "limit scope is disabled"),
        ("chat", "limit scope is disabled or invalid"),
        ("unknown_scope", "limit scope is disabled or invalid"),
    ],
)
async def test_reactivate_manual_lot_rejects_unavailable_catalog(
    auth_client: AsyncClient,
    session: AsyncSession,
    unavailable_catalog: str,
    expected_detail: str,
):
    tier, duration, scope = await _seed_catalog(session)
    tier.is_sellable = True
    if unavailable_catalog == "tier_inactive":
        tier.is_active = False
    elif unavailable_catalog == "tier_unsellable":
        tier.is_sellable = False
    elif unavailable_catalog == "funpay_form":
        tier.code = "enterprise"
    elif unavailable_catalog == "duration":
        duration.is_enabled = False
    elif unavailable_catalog == "scope":
        scope.is_enabled = False
    elif unavailable_catalog == "chat":
        scope.code = "chat"
        scope.is_enabled = True
    else:
        scope.code = "legacy"
        scope.is_enabled = True
    lot = Lot(
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Тест",
        title_en="Test",
        status="paused",
        paused_reason="manual",
        auto_created=False,
        funpay_id="700",
    )
    session.add(lot)
    await session.commit()
    lifecycle = _Lifecycle(session)
    app.state.lifecycle = lifecycle
    try:
        response = await auth_client.patch(
            f"/api/lots/{lot.id}",
            json={"status": "active"},
        )
    finally:
        del app.state.lifecycle

    assert response.status_code == 422
    assert expected_detail in response.json()["detail"]
    await session.refresh(lot)
    assert lot.status == "paused"
    assert lifecycle.synced == []


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
    assert lifecycle.deleted == [lot_id]
    assert lifecycle.synced == [(lot_id, True)]


async def test_delete_lot_keeps_local_state_when_remote_delete_fails(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    class _RejectingLifecycle(_Lifecycle):
        async def delete_lot(self, lot_id: int):
            raise RuntimeError("remote deletion was not verified")

    tier, duration, scope = await _seed_catalog(session)
    tier.is_sellable = True
    lot = Lot(
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Т",
        title_en="T",
        status="active",
        auto_created=False,
        funpay_id="700",
    )
    session.add(lot)
    await session.commit()
    lifecycle = _RejectingLifecycle(session)
    app.state.lifecycle = lifecycle
    try:
        response = await auth_client.delete(f"/api/lots/{lot.id}")
    finally:
        del app.state.lifecycle

    assert response.status_code == 502
    await session.refresh(lot)
    assert lot.status == "active"
    assert lifecycle.deleted == []


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
