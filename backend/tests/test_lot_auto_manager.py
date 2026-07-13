from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import PriceMatrix, Lot
from app.services.lot_auto_manager import LotAutoManager


async def _seed_catalog(session: AsyncSession):
    tier = SubscriptionTier(
        code="plus", name="Plus", is_active=True, is_sellable=True,
    )
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope_any = LimitScope(code="any", name="Любой")
    session.add(scope_any)
    await session.flush()
    return tier, duration, scope_any


async def _add_account_with_limits(
    session: AsyncSession,
    tier_id: int,
    n: int = 1,
    *,
    expires_in_days: int | None = 30,
    codex_primary: int | None = None,
    codex_window_seconds: int | None = None,
):
    tier = await session.get(SubscriptionTier, tier_id)
    assert tier is not None
    acc = Account(
        login=f"acc{n}",
        password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=tier_id, status="active",
        subscription_expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            if expires_in_days is not None
            else None
        ),
    )
    session.add(acc)
    await session.flush()
    session.add(AccountLimits(
        account_id=acc.id, refresh_token_encrypted="enc",
        chat_5h_remaining_pct=80, chat_weekly_remaining_pct=70,
        codex_5h_remaining_pct=60, codex_weekly_remaining_pct=50,
        codex_primary_remaining_pct=codex_primary,
        codex_primary_window_seconds=codex_window_seconds,
        measured_at=datetime.now(timezone.utc), refresh_status="ok",
        plan_type=tier.code or "plus",
        plan_window_status="ok",
        expected_long_window_seconds=(
            30 * 24 * 60 * 60 if tier.code == "free" else 7 * 24 * 60 * 60
        ),
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


async def test_successful_first_create_is_durable_when_second_remote_call_fails(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    second_duration = Duration(days=3, is_enabled=True, sort_order=5)
    session.add(second_duration)
    await session.flush()
    session.add_all([
        PriceMatrix(
            tier_id=tier.id,
            duration_id=duration.id,
            limit_scope_id=scope.id,
            price=599,
        ),
        PriceMatrix(
            tier_id=tier.id,
            duration_id=second_duration.id,
            limit_scope_id=scope.id,
            price=399,
        ),
    ])
    await session.commit()

    class FailSecondCreateGateway(FakeChatGateway):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def save_offer_fields(self, fields):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("second create failed")
            return await super().save_offer_fields(fields)

    with pytest.raises(RuntimeError, match="second create failed"):
        await LotAutoManager(55).run(session, FailSecondCreateGateway())
    await session.rollback()

    lots = list((await session.execute(select(Lot))).scalars())
    assert len(lots) == 1
    assert lots[0].funpay_id is not None


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
    assert gateway.saved_offers[100].active is False


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
    assert gateway.saved_offers[200].active is True


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


@pytest.mark.parametrize("disabled_catalog", ["tier", "duration"])
async def test_disabled_catalog_row_pauses_existing_auto_lot(
    session: AsyncSession,
    disabled_catalog: str,
):
    tier, duration, scope = await _seed_catalog(session)
    matrix = PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    )
    session.add(matrix)
    await session.flush()
    lot = Lot(
        config_key=matrix.config_key,
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="T",
        title_en="T",
        status="active",
        auto_created=True,
        funpay_id="601",
    )
    session.add(lot)
    if disabled_catalog == "tier":
        tier.is_sellable = False
    else:
        duration.is_enabled = False
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert any(action.action == "pause" for action in actions)
    assert lot.status == "paused"
    assert lot.paused_reason == "auto_no_config"


@pytest.mark.parametrize(
    ("tier_code", "window_seconds", "expires_in_days"),
    [
        ("free", 30 * 24 * 60 * 60, None),
        ("plus", 7 * 24 * 60 * 60, 30),
    ],
)
async def test_codex_capacity_uses_exact_plan_window(
    session: AsyncSession,
    tier_code: str,
    window_seconds: int,
    expires_in_days: int | None,
):
    """Free 30-day and paid 7-day windows are both real long windows."""
    tier, duration, _scope_any = await _seed_catalog(session)
    tier.code = tier_code
    scope = LimitScope(code="codex", name=f"Codex {tier_code}")
    session.add(scope)
    await session.flush()
    await _add_account_with_limits(
        session,
        tier.id,
        expires_in_days=expires_in_days,
        codex_primary=95,
        codex_window_seconds=window_seconds,
    )
    # Exact-window rows intentionally do not contain a fabricated legacy value.
    limits = (await session.execute(select(AccountLimits))).scalar_one()
    limits.codex_5h_remaining_pct = None
    limits.codex_weekly_remaining_pct = None
    matrix = PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        min_limit_pct=90,
        price=599,
    )
    session.add(matrix)
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert any(action.action == "create" for action in actions)
    lot = (await session.execute(select(Lot))).scalar_one()
    assert f"Codex ≥ {matrix.min_limit_pct}%" in lot.title_ru
    assert "30 дней только на Free, 7 дней на платных" in lot.description_ru
    assert "фактический остаток OpenAI" in lot.description_ru


@pytest.mark.parametrize(
    (
        "tier_code",
        "primary_pct",
        "primary_seconds",
        "secondary_pct",
        "secondary_seconds",
        "max_5h",
        "max_long",
        "has_capacity",
    ),
    [
        ("free", 80, 30 * 24 * 60 * 60, None, None, None, 90, True),
        ("free", 80, 30 * 24 * 60 * 60, None, None, None, 70, False),
        ("free", 80, 30 * 24 * 60 * 60, None, None, 30, 90, False),
        ("plus", 20, 5 * 60 * 60, 80, 7 * 24 * 60 * 60, 30, 90, True),
        ("plus", 20, 5 * 60 * 60, 80, 7 * 24 * 60 * 60, 10, 90, False),
    ],
)
async def test_any_capacity_ceilings_follow_short_and_long_semantics(
    session: AsyncSession,
    tier_code: str,
    primary_pct: int,
    primary_seconds: int,
    secondary_pct: int | None,
    secondary_seconds: int | None,
    max_5h: int | None,
    max_long: int,
    has_capacity: bool,
):
    tier, duration, scope = await _seed_catalog(session)
    tier.code = tier_code
    account = await _add_account_with_limits(
        session,
        tier.id,
        expires_in_days=None if tier_code == "free" else 30,
    )
    limits = await session.get(AccountLimits, account.id)
    limits.codex_5h_remaining_pct = None
    limits.codex_weekly_remaining_pct = None
    limits.codex_primary_remaining_pct = primary_pct
    limits.codex_primary_window_seconds = primary_seconds
    limits.codex_secondary_remaining_pct = secondary_pct
    limits.codex_secondary_window_seconds = secondary_seconds
    limits.expected_long_window_seconds = (
        30 * 24 * 60 * 60 if tier_code == "free" else 7 * 24 * 60 * 60
    )
    # There is no trustworthy ChatGPT usage endpoint; exact-window capacity
    # here is intentionally based only on observed Codex data.
    limits.chat_5h_remaining_pct = None
    limits.chat_weekly_remaining_pct = None
    session.add(
        PriceMatrix(
            tier_id=tier.id,
            duration_id=duration.id,
            limit_scope_id=scope.id,
            max_5h_pct=max_5h,
            max_weekly_pct=max_long,
            price=599,
        )
    )
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert any(action.action == "create" for action in actions) is has_capacity
