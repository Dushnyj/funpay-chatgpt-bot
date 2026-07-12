from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import PriceMatrix, Lot
from app.services.lot_auto_manager import LotAutoManager


async def _seed_catalog(session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope_any = LimitScope(code="any", name="Любой")
    session.add(scope_any)
    await session.flush()
    return tier, duration, scope_any


async def _add_account_with_limits(session: AsyncSession, tier_id: int, n: int = 1):
    acc = Account(
        login=f"acc{n}",
        password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=tier_id, status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    session.add(AccountLimits(
        account_id=acc.id, refresh_token_encrypted="enc",
        chat_5h_remaining_pct=80, chat_weekly_remaining_pct=70,
        codex_5h_remaining_pct=60, codex_weekly_remaining_pct=50,
        measured_at=datetime.now(timezone.utc), refresh_status="ok",
    ))
    await session.flush()
    return acc


async def test_creates_lot_when_capacity_available(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    session.add(PriceMatrix(
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        price=599,
    ))
    await session.flush()

    gateway = FakeChatGateway()
    mgr = LotAutoManager(funpay_node_id=55)
    actions = await mgr.run(session, gateway)
    assert any(a.action == "create" for a in actions)


async def test_pauses_lot_when_no_capacity(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    # Нет аккаунтов — capacity = 0
    session.add(PriceMatrix(
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        price=599,
    ))
    lot = Lot(
        funpay_node_id=55, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=599, title_ru="T", title_en="T",
        status="active", auto_created=True, funpay_id="100",
    )
    session.add(lot)
    await session.flush()

    gateway = FakeChatGateway()
    mgr = LotAutoManager(funpay_node_id=55)
    actions = await mgr.run(session, gateway)
    assert any(a.action == "pause" for a in actions)
    await session.refresh(lot)
    assert lot.status == "paused"
    assert (100, False) in gateway.activity_changes


async def test_activates_lot_when_capacity_returns(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    session.add(PriceMatrix(
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        price=599,
    ))
    lot = Lot(
        funpay_node_id=55, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=599, title_ru="T", title_en="T",
        status="paused", auto_created=True, funpay_id="200",
    )
    session.add(lot)
    await session.flush()

    gateway = FakeChatGateway()
    mgr = LotAutoManager(funpay_node_id=55)
    actions = await mgr.run(session, gateway)
    assert any(a.action == "activate" for a in actions)
    await session.refresh(lot)
    assert lot.status == "active"
    assert (200, True) in gateway.activity_changes


async def test_full_account_capacity_pauses_lot(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    account = await _add_account_with_limits(session, tier.id)
    account.max_active_rentals = 0
    matrix = PriceMatrix(
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        price=599,
    )
    session.add(matrix)
    await session.flush()
    lot = Lot(
        config_key=matrix.config_key, funpay_node_id=55, tier_id=tier.id,
        duration_id=duration.id, limit_scope_id=scope.id, price=599,
        title_ru="T", title_en="T", status="active", auto_created=True,
        funpay_id="300",
    )
    session.add(lot)
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert any(action.action == "pause" for action in actions)


async def test_price_change_is_synced_to_existing_offer(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    matrix = PriceMatrix(
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        price=799,
    )
    session.add(matrix)
    await session.flush()
    lot = Lot(
        config_key=matrix.config_key, funpay_node_id=55, tier_id=tier.id,
        duration_id=duration.id, limit_scope_id=scope.id, price=599,
        title_ru="T", title_en="T", status="active", auto_created=True,
        funpay_id="400",
    )
    session.add(lot)
    await session.flush()
    gateway = FakeChatGateway()

    actions = await LotAutoManager(55).run(session, gateway)

    assert any(action.action == "update" for action in actions)
    assert gateway.saved_offers[400].price == 799


async def test_manual_pause_is_not_automatically_reactivated(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    matrix = PriceMatrix(
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        price=599,
    )
    session.add(matrix)
    await session.flush()
    lot = Lot(
        config_key=matrix.config_key, funpay_node_id=55, tier_id=tier.id,
        duration_id=duration.id, limit_scope_id=scope.id, price=599,
        title_ru="T", title_en="T", status="paused", paused_reason="manual",
        auto_created=True, funpay_id="500",
    )
    session.add(lot)
    await session.flush()
    gateway = FakeChatGateway()

    await LotAutoManager(55).run(session, gateway)

    assert lot.status == "paused"
    assert gateway.activity_changes == []


async def test_removed_price_config_pauses_orphaned_lot(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    lot = Lot(
        funpay_node_id=55, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=599, title_ru="T", title_en="T",
        status="active", auto_created=True, funpay_id="600",
    )
    session.add(lot)
    await session.flush()
    gateway = FakeChatGateway()

    actions = await LotAutoManager(55).run(session, gateway)

    assert any(action.action == "pause" for action in actions)
    assert lot.paused_reason == "auto_no_config"
