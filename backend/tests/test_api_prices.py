import pytest
from unittest.mock import AsyncMock
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import PriceMatrix


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_update_and_get_prices(auth_client: AsyncClient, session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True, is_sellable=True)
    session.add(tier)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()

    resp = await auth_client.put("/api/prices", json={
        "items": [
            {"tier_id": tier.id, "duration_id": duration.id, "limit_scope_id": scope.id, "price": 599},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1

    resp = await auth_client.get("/api/prices")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["price"] == 599


async def test_price_update_triggers_immediate_lot_reconciliation(
    auth_client: AsyncClient, session: AsyncSession,
):
    tier = SubscriptionTier(name="Plus", is_active=True, is_sellable=True)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, duration, scope])
    await session.flush()
    lifecycle = type("Lifecycle", (), {"reconcile_lots": AsyncMock(return_value=[])})()
    app.state.lifecycle = lifecycle
    try:
        response = await auth_client.put("/api/prices", json={
            "items": [{
                "tier_id": tier.id,
                "duration_id": duration.id,
                "limit_scope_id": scope.id,
                "price": 699,
            }],
        })
    finally:
        del app.state.lifecycle

    assert response.status_code == 200
    lifecycle.reconcile_lots.assert_awaited_once_with()


async def test_price_matrix_can_be_cleared(
    auth_client: AsyncClient, session: AsyncSession,
):
    tier = SubscriptionTier(name="Plus", is_active=True, is_sellable=True)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, duration, scope])
    await session.flush()
    first = await auth_client.put("/api/prices", json={
        "items": [{
            "tier_id": tier.id,
            "duration_id": duration.id,
            "limit_scope_id": scope.id,
            "price": 599,
        }],
    })
    assert first.status_code == 200

    cleared = await auth_client.put("/api/prices", json={"items": []})

    assert cleared.status_code == 200
    assert cleared.json() == {"updated": 0}
    assert (await auth_client.get("/api/prices")).json() == []


async def test_guaranteed_scope_requires_minimum_and_rejects_legacy_ceilings(
    auth_client: AsyncClient, session: AsyncSession,
):
    tier = SubscriptionTier(name="Plus", is_active=True, is_sellable=True)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="codex", name="Codex")
    session.add_all([tier, duration, scope])
    await session.flush()

    missing_minimum = await auth_client.put("/api/prices", json={
        "items": [{
            "tier_id": tier.id,
            "duration_id": duration.id,
            "limit_scope_id": scope.id,
            "price": 599,
        }],
    })
    invalid_ceiling = await auth_client.put("/api/prices", json={
        "items": [{
            "tier_id": tier.id,
            "duration_id": duration.id,
            "limit_scope_id": scope.id,
            "min_limit_pct": 50,
            "max_weekly_pct": 80,
            "price": 599,
        }],
    })

    assert missing_minimum.status_code == 422
    assert "requires a minimum" in missing_minimum.json()["detail"]
    assert invalid_ceiling.status_code == 422
    assert "cannot use maximum" in invalid_ceiling.json()["detail"]


async def test_chat_guarantee_is_rejected_when_openai_does_not_publish_usage(
    auth_client: AsyncClient, session: AsyncSession,
):
    tier = SubscriptionTier(name="Plus", is_active=True, is_sellable=True)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="chat", name="ChatGPT")
    session.add_all([tier, duration, scope])
    await session.flush()

    response = await auth_client.put("/api/prices", json={
        "items": [{
            "tier_id": tier.id,
            "duration_id": duration.id,
            "limit_scope_id": scope.id,
            "min_limit_pct": 50,
            "price": 599,
        }],
    })

    assert response.status_code == 422
    assert "limit scope is disabled or invalid" in response.json()["detail"]


async def test_price_rejects_legacy_five_hour_ceiling_for_every_plan(
    auth_client: AsyncClient, session: AsyncSession,
):
    tier = SubscriptionTier(
        code="plus", name="Plus", is_active=True, is_sellable=True,
    )
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, duration, scope])
    await session.flush()

    response = await auth_client.put("/api/prices", json={
        "items": [{
            "tier_id": tier.id,
            "duration_id": duration.id,
            "limit_scope_id": scope.id,
            "max_5h_pct": 30,
            "max_weekly_pct": 90,
            "price": 599,
        }],
    })

    assert response.status_code == 422
    assert "5-hour condition is legacy-only" in response.json()["detail"]


@pytest.mark.parametrize("catalog_state", ["tier", "duration"])
async def test_full_matrix_save_preserves_temporarily_disabled_catalog_rows(
    auth_client: AsyncClient,
    session: AsyncSession,
    catalog_state: str,
):
    tier = SubscriptionTier(
        code="plus",
        name="Plus",
        is_active=catalog_state != "tier",
        is_sellable=catalog_state != "tier",
    )
    duration = Duration(
        minutes=7 * 24 * 60,
        is_enabled=catalog_state != "duration",
        sort_order=10,
    )
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, duration, scope])
    await session.flush()

    response = await auth_client.put("/api/prices", json={
        "items": [{
            "tier_id": tier.id,
            "duration_id": duration.id,
            "limit_scope_id": scope.id,
            "price": 599,
        }],
    })

    assert response.status_code == 200
    assert response.json() == {"updated": 1}
    listed = (await auth_client.get("/api/prices")).json()
    assert len(listed) == 1
    assert listed[0]["duration_id"] == duration.id


@pytest.mark.parametrize("scope_code", ["any", "codex"])
async def test_full_matrix_save_preserves_disabled_supported_scope(
    auth_client: AsyncClient,
    session: AsyncSession,
    scope_code: str,
):
    tier = SubscriptionTier(
        code="plus",
        name="Plus",
        is_active=True,
        is_sellable=True,
    )
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    scope = LimitScope(
        code=scope_code,
        name=scope_code.title(),
        is_enabled=False,
    )
    session.add_all([tier, duration, scope])
    await session.flush()
    item = {
        "tier_id": tier.id,
        "duration_id": duration.id,
        "limit_scope_id": scope.id,
        "price": 599,
    }
    if scope_code == "codex":
        item["min_limit_pct"] = 50

    response = await auth_client.put("/api/prices", json={"items": [item]})

    assert response.status_code == 200
    assert response.json() == {"updated": 1}
    listed = (await auth_client.get("/api/prices")).json()
    assert len(listed) == 1
    assert listed[0]["limit_scope_id"] == scope.id


async def test_full_matrix_save_preserves_hidden_legacy_scope_tombstone(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    tier = SubscriptionTier(
        code="plus",
        name="Plus",
        is_active=True,
        is_sellable=True,
    )
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    canonical_scope = LimitScope(code="any", name="Любой")
    legacy_scope = LimitScope(
        code="chat",
        name="ChatGPT",
        is_enabled=False,
    )
    session.add_all([tier, duration, canonical_scope, legacy_scope])
    await session.flush()
    legacy_price = PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=legacy_scope.id,
        price=499,
    )
    session.add(legacy_price)
    await session.commit()

    response = await auth_client.put("/api/prices", json={
        "items": [{
            "tier_id": tier.id,
            "duration_id": duration.id,
            "limit_scope_id": canonical_scope.id,
            "price": 599,
        }],
    })

    assert response.status_code == 200
    rows = (await session.execute(select(PriceMatrix))).scalars().all()
    assert {(row.limit_scope_id, row.price) for row in rows} == {
        (legacy_scope.id, 499),
        (canonical_scope.id, 599),
    }
    # Legacy compatibility data stays recoverable in the database, but the
    # active editor/API must never advertise the unsupported Chat guarantee.
    assert (await auth_client.get("/api/prices")).json() == [{
        "tier_id": tier.id,
        "duration_id": duration.id,
        "limit_scope_id": canonical_scope.id,
        "min_limit_pct": None,
        "max_5h_pct": None,
        "max_weekly_pct": None,
        "price": 599,
    }]
