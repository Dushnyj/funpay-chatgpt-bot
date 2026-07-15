from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.exceptions import FunPayOfferResolutionError
from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.provenance import public_provenance_code
from app.integrations.funpay.types import OfferInfo
from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.audit import AuditLog
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot, LotTemplate, PriceMatrix
from app.models.message import MessageTemplate
from app.models.rental import Order, Rental
from app.services.lot_auto_manager import (
    LotAutoManager,
    ProvenanceMarkerSyncError,
)


async def test_marker_startup_barrier_isolates_lot_failures(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    active = Lot(
        funpay_id="101",
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        title_ru="Active",
        title_en="Active",
        status="active",
        provenance_marker_synced=False,
        config_key="marker-active",
    )
    paused = Lot(
        funpay_id="102",
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        title_ru="Paused",
        title_en="Paused",
        status="paused",
        provenance_marker_synced=False,
        config_key="marker-paused",
    )
    session.add_all([active, paused])
    await session.commit()

    class OneBrokenOfferGateway(FakeChatGateway):
        async def save_offer_fields(self, fields):
            if fields.offer_id == 101:
                raise RuntimeError("remote update failed")
            return await super().save_offer_fields(fields)

    with pytest.raises(ProvenanceMarkerSyncError):
        await LotAutoManager(55).sync_missing_provenance_markers(
            session,
            OneBrokenOfferGateway(),
            strict=True,
        )

    await session.refresh(active)
    await session.refresh(paused)
    assert active.provenance_marker_synced is False
    assert paused.provenance_marker_synced is True


async def _seed_catalog(session: AsyncSession):
    session.add_all([
        MessageTemplate(
            key="payment_received",
            lang="ru",
            content="✅ Оплата получена. Данные придут в этот чат.",
        ),
        MessageTemplate(
            key="payment_received",
            lang="en",
            content="✅ Payment received. Access details will arrive here.",
        ),
    ])
    tier = SubscriptionTier(
        code="plus", name="Plus", is_active=True, is_sellable=True,
    )
    session.add(tier)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
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
    codex_primary: int | None = 50,
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
        subscription_expiry_source=(
            "accounts_check" if tier.code != "free" else None
        ),
    )
    session.add(acc)
    await session.flush()
    expected_long_window = (
        30 * 24 * 60 * 60 if tier.code == "free" else 7 * 24 * 60 * 60
    )
    session.add(AccountLimits(
        account_id=acc.id, refresh_token_encrypted="enc",
        codex_5h_remaining_pct=60, codex_weekly_remaining_pct=50,
        codex_primary_remaining_pct=codex_primary,
        codex_primary_window_seconds=(
            codex_window_seconds
            if codex_window_seconds is not None
            else expected_long_window
        ),
        measured_at=datetime.now(timezone.utc), refresh_status="ok",
        plan_type=tier.code or "plus",
        plan_window_status="ok",
        expected_long_window_seconds=expected_long_window,
    ))
    await session.flush()
    return acc


def _expose_saved_offer(gateway: FakeChatGateway) -> int:
    offer_id, fields = next(iter(gateway.saved_offers.items()))
    gateway.set_my_offers(55, [OfferInfo(
        offer_id=offer_id,
        subcategory_id=55,
        title=f"{fields.title_ru}, С подпиской",
        price=fields.price,
        active=fields.active,
        auto_delivery=fields.auto_delivery,
    )])
    return offer_id


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


async def test_does_not_publish_paid_lot_for_unsourced_expiry(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    account = await _add_account_with_limits(session, tier.id)
    account.subscription_expiry_source = None
    session.add(PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    ))
    await session.flush()

    actions = await LotAutoManager(funpay_node_id=55).run(
        session,
        FakeChatGateway(),
    )

    assert actions == []
    assert (await session.execute(select(Lot))).scalars().all() == []


async def test_disabled_limit_scope_is_excluded_from_automatic_lots(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    scope.is_enabled = False
    await _add_account_with_limits(session, tier.id)
    session.add(PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    ))
    await session.flush()

    gateway = FakeChatGateway()
    actions = await LotAutoManager(funpay_node_id=55).run(session, gateway)

    assert actions == []
    lots = (await session.execute(select(Lot))).scalars().all()
    assert lots == []


async def test_unknown_limit_scope_is_excluded_from_automatic_lots(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    scope.code = "legacy"
    scope.is_enabled = True
    await _add_account_with_limits(session, tier.id)
    session.add(PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    ))
    await session.flush()

    actions = await LotAutoManager(funpay_node_id=55).run(
        session,
        FakeChatGateway(),
    )

    assert actions == []
    lots = (await session.execute(select(Lot))).scalars().all()
    assert lots == []


async def test_uses_most_specific_enabled_lot_template_deterministically(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    session.add_all(
        [
            PriceMatrix(
                tier_id=tier.id,
                duration_id=duration.id,
                limit_scope_id=scope.id,
                min_limit_pct=70,
                price=599,
            ),
            LotTemplate(
                key="general",
                name="General",
                title_template_ru="GENERAL {plan} {days} {condition}",
                title_template_en="GENERAL {plan} {days} {condition}",
                description_template_ru="Общий {long_window_days}",
                description_template_en="General {long_window_days}",
                is_enabled=True,
                system_managed=False,
            ),
            LotTemplate(
                key="specific",
                name="Specific",
                tier_id=tier.id,
                limit_scope_id=scope.id,
                title_template_ru="SPECIFIC {plan} {days} {condition}",
                title_template_en="SPECIFIC {plan} {days} {condition}",
                description_template_ru=(
                    "Точный {long_window_days}; "
                    "{min_limit}/{short_limit}/{long_limit}"
                ),
                description_template_en=(
                    "Specific {long_window_days}; "
                    "{min_limit}/{short_limit}/{long_limit}"
                ),
                is_enabled=True,
                system_managed=False,
            ),
        ]
    )
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert any(action.action == "create" for action in actions)
    lot = (await session.execute(select(Lot))).scalar_one()
    assert lot.title_ru.startswith("SPECIFIC")
    assert lot.description_ru == "Точный 7; 70%/—/—"


async def test_successful_first_create_is_durable_when_second_remote_call_fails(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    second_duration = Duration(minutes=3 * 24 * 60, is_enabled=True, sort_order=5)
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
    assert len(lots) == 2
    bound = [lot for lot in lots if lot.funpay_id is not None]
    pending = [lot for lot in lots if lot.funpay_id is None]
    assert len(bound) == 1
    assert bound[0].status == "active"
    assert len(pending) == 1
    assert pending[0].status == "paused"
    assert pending[0].paused_reason == "auto_publish_pending"


async def test_remote_save_resolution_failure_recovers_same_inactive_offer(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    session.add(PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    ))
    await session.commit()

    class AcceptedButUnresolvedGateway(FakeChatGateway):
        fail_resolution = True

        async def save_offer_fields(self, fields):
            offer_id = await super().save_offer_fields(fields)
            if fields.offer_id == 0 and self.fail_resolution:
                raise FunPayOfferResolutionError("preview not visible")
            return offer_id

    gateway = AcceptedButUnresolvedGateway()
    manager = LotAutoManager(55)

    with pytest.raises(FunPayOfferResolutionError, match="preview not visible"):
        await manager.run(session, gateway)

    pending = (await session.execute(select(Lot))).scalar_one()
    token = pending.provenance_token
    assert pending.funpay_id is None
    assert pending.status == "paused"
    assert pending.paused_reason == "auto_publish_pending"
    offer_id = _expose_saved_offer(gateway)
    assert gateway.saved_offers[offer_id].active is False
    assert public_provenance_code(token) in gateway.saved_offers[offer_id].desc_ru

    gateway.fail_resolution = False
    actions = await manager.run(session, gateway)

    await session.refresh(pending)
    assert [(action.lot_id, action.action) for action in actions] == [
        (pending.id, "activate")
    ]
    assert pending.funpay_id == str(offer_id)
    assert pending.provenance_token == token
    assert pending.status == "active"
    assert set(gateway.saved_offers) == {offer_id}
    assert gateway.saved_offers[offer_id].active is True


async def test_binding_commit_failure_recovers_without_second_remote_create(
    session: AsyncSession,
    monkeypatch,
):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    session.add(PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    ))
    await session.commit()
    gateway = FakeChatGateway()
    manager = LotAutoManager(55)
    real_commit = session.commit
    commit_count = 0

    async def fail_binding_commit():
        nonlocal commit_count
        commit_count += 1
        if commit_count == 2:
            await session.rollback()
            raise RuntimeError("binding commit interrupted")
        await real_commit()

    monkeypatch.setattr(session, "commit", fail_binding_commit)
    with pytest.raises(RuntimeError, match="binding commit interrupted"):
        await manager.run(session, gateway)

    pending = (await session.execute(select(Lot))).scalar_one()
    token = pending.provenance_token
    assert pending.funpay_id is None
    offer_id = _expose_saved_offer(gateway)
    assert gateway.saved_offers[offer_id].active is False

    monkeypatch.setattr(session, "commit", real_commit)
    await manager.run(session, gateway)

    await session.refresh(pending)
    assert pending.funpay_id == str(offer_id)
    assert pending.provenance_token == token
    assert pending.status == "active"
    assert set(gateway.saved_offers) == {offer_id}


async def test_activation_failure_keeps_durable_inactive_binding_for_retry(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    session.add(PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    ))
    await session.commit()

    class ActivationFailureGateway(FakeChatGateway):
        fail_activation = True

        async def set_offer_active(self, offer_id: int, active: bool) -> bool:
            if active and self.fail_activation:
                raise RuntimeError("activation interrupted")
            return await super().set_offer_active(offer_id, active)

    gateway = ActivationFailureGateway()
    manager = LotAutoManager(55)

    with pytest.raises(RuntimeError, match="activation interrupted"):
        await manager.run(session, gateway)

    pending = (await session.execute(select(Lot))).scalar_one()
    offer_id = int(pending.funpay_id or 0)
    assert offer_id > 0
    assert pending.status == "paused"
    assert pending.paused_reason == "auto_publish_pending"
    assert pending.provenance_marker_synced is True
    assert gateway.saved_offers[offer_id].active is False

    gateway.fail_activation = False
    await manager.run(session, gateway)

    await session.refresh(pending)
    assert pending.funpay_id == str(offer_id)
    assert pending.status == "active"
    assert gateway.saved_offers[offer_id].active is True
    assert set(gateway.saved_offers) == {offer_id}


async def test_template_render_error_does_not_block_other_lot_and_is_audited(
    session: AsyncSession,
):
    valid_tier, duration, scope = await _seed_catalog(session)
    invalid_tier = SubscriptionTier(
        code="pro_20x",
        name="X" * 120,
        is_active=True,
        is_sellable=True,
    )
    session.add(invalid_tier)
    await session.flush()
    await _add_account_with_limits(session, valid_tier.id, n=1)
    await _add_account_with_limits(session, invalid_tier.id, n=2)
    valid_matrix = PriceMatrix(
        tier_id=valid_tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    )
    invalid_matrix = PriceMatrix(
        tier_id=invalid_tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=799,
    )
    session.add_all(
        [
            valid_matrix,
            invalid_matrix,
            LotTemplate(
                key="invalid-render",
                name="Invalid render",
                tier_id=invalid_tier.id,
                title_template_ru=("Y" * 140) + " {plan} {days} {condition}",
                title_template_en=("Y" * 140) + " {plan} {days} {condition}",
                description_template_ru="",
                description_template_en="",
                is_enabled=True,
                system_managed=False,
            ),
        ]
    )
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert [action.action for action in actions].count("create") == 1
    lot = (await session.execute(select(Lot))).scalar_one()
    assert lot.tier_id == valid_tier.id
    audit = (await session.execute(select(AuditLog))).scalar_one()
    assert audit.event_type == "lot_template_render_failed"
    assert audit.metadata_["config_key"] == invalid_matrix.config_key
    assert "255" in audit.metadata_["error"]


async def test_existing_active_lot_with_template_render_error_is_paused_and_audited(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    tier.name = "X" * 120
    await _add_account_with_limits(session, tier.id)
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
        title_ru="Old",
        title_en="Old",
        status="active",
        auto_created=True,
        funpay_id="701",
    )
    session.add_all(
        [
            lot,
            LotTemplate(
                key="invalid-existing-render",
                name="Invalid existing render",
                tier_id=tier.id,
                title_template_ru=("Y" * 140) + " {plan} {days} {condition}",
                title_template_en=("Y" * 140) + " {plan} {days} {condition}",
                description_template_ru="",
                description_template_en="",
                is_enabled=True,
                system_managed=False,
            ),
        ]
    )
    await session.flush()
    gateway = FakeChatGateway()

    actions = await LotAutoManager(55).run(session, gateway)

    await session.refresh(lot)
    assert any(
        action.lot_id == lot.id and action.action == "pause"
        for action in actions
    )
    assert lot.status == "paused"
    assert lot.paused_reason == "auto_template_error"
    assert gateway.activity_changes == [(701, False)]
    audit = (await session.execute(select(AuditLog))).scalar_one()
    assert audit.event_type == "lot_template_render_failed"
    assert audit.metadata_["config_key"] == matrix.config_key


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


@pytest.mark.parametrize("limits_state", ["stale", "missing", "expired"])
async def test_pauses_lot_when_account_limits_are_unusable(
    session: AsyncSession,
    limits_state: str,
):
    tier, duration, scope = await _seed_catalog(session)
    account = await _add_account_with_limits(session, tier.id)
    limits = await session.get(AccountLimits, account.id)
    assert limits is not None
    if limits_state == "stale":
        limits.measured_at = (
            datetime.now(timezone.utc) - timedelta(hours=1, seconds=1)
        )
    elif limits_state == "missing":
        limits.measured_at = None
    else:
        limits.refresh_status = "expired"
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
        funpay_id="101",
    )
    session.add(lot)
    await session.flush()

    gateway = FakeChatGateway()
    actions = await LotAutoManager(funpay_node_id=55).run(session, gateway)

    assert any(action.action == "pause" for action in actions)
    await session.refresh(lot)
    assert lot.status == "paused"
    assert lot.paused_reason == "auto_no_account"
    assert gateway.saved_offers[101].active is False


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


async def test_capacity_refresh_republishes_single_unit_but_periodic_run_is_idle(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id)
    session.add(PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    ))
    await session.flush()
    gateway = FakeChatGateway()
    manager = LotAutoManager(funpay_node_id=55)

    created = await manager.run(session, gateway)
    assert [action.action for action in created] == ["create"]
    lot = (await session.execute(select(Lot))).scalar_one()
    offer_id = int(lot.funpay_id or 0)
    gateway.saved_offers[offer_id] = replace(
        gateway.saved_offers[offer_id],
        amount=0,
    )

    periodic = await manager.run(session, gateway)

    assert [action.action for action in periodic] == ["none"]
    assert gateway.saved_offers[offer_id].amount == 0

    capacity_change = await manager.run(
        session,
        gateway,
        refresh_stock=True,
    )

    assert [action.action for action in capacity_change] == ["update"]
    assert gateway.saved_offers[offer_id].amount == 1
    assert gateway.saved_offers[offer_id].active is True


@pytest.mark.parametrize("rental_status", ["active", "expiry_pending"])
async def test_full_account_capacity_pauses_lot(
    session: AsyncSession,
    rental_status: str,
):
    tier, duration, scope = await _seed_catalog(session)
    account = await _add_account_with_limits(session, tier.id)
    order = Order(
        funpay_order_id="capacity-order",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        status="completed",
    )
    session.add(order)
    await session.flush()
    session.add(
        Rental(
            order_id=order.id,
            account_id=account.id,
            buyer_funpay_id="200",
            buyer_funpay_chat_id="100",
            tier_id=tier.id,
            duration_id=duration.id,
            limit_scope_id=scope.id,
            lang="ru",
            started_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            status=rental_status,
            credentials_delivery_status="sent",
        )
    )
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


async def test_replacement_reservation_consumes_old_and_target_capacity(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    old_account = await _add_account_with_limits(session, tier.id, n=1)
    target_account = await _add_account_with_limits(session, tier.id, n=2)
    order = Order(
        funpay_order_id="reserved-capacity-order",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        status="completed",
    )
    session.add(order)
    await session.flush()
    session.add(
        Rental(
            order_id=order.id,
            account_id=old_account.id,
            replacement_target_account_id=target_account.id,
            buyer_funpay_id="200",
            buyer_funpay_chat_id="100",
            tier_id=tier.id,
            duration_id=duration.id,
            limit_scope_id=scope.id,
            lang="ru",
            started_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            status="active",
            credentials_delivery_status="sent",
        )
    )
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
        funpay_id="310",
    )
    session.add(lot)
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert any(action.action == "pause" for action in actions)
    await session.refresh(lot)
    assert lot.status == "paused"
    assert lot.paused_reason == "auto_no_account"


@pytest.mark.parametrize("job_status", ["pending", "running"])
async def test_active_account_check_job_consumes_lot_capacity(
    session: AsyncSession,
    job_status: str,
):
    tier, duration, scope = await _seed_catalog(session)
    account = await _add_account_with_limits(session, tier.id)
    session.add(
        AccountCheckJob(
            account_id=account.id,
            priority="limit_check",
            job_type="limit_check",
            status=job_status,
            started_at=(
                datetime.now(timezone.utc) if job_status == "running" else None
            ),
        )
    )
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
        funpay_id="311",
    )
    session.add(lot)
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert any(action.action == "pause" for action in actions)
    await session.refresh(lot)
    assert lot.status == "paused"
    assert lot.paused_reason == "auto_no_account"


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


async def test_disabled_limit_scope_pauses_existing_auto_lot(
    session: AsyncSession,
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
        funpay_id="602",
    )
    session.add(lot)
    scope.is_enabled = False
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert any(action.action == "pause" for action in actions)
    assert lot.status == "paused"
    assert lot.paused_reason == "auto_no_config"


@pytest.mark.parametrize(
    "unavailable_catalog",
    [
        "tier_inactive",
        "tier_unsellable",
        "funpay_form",
        "duration",
        "scope",
        "chat",
        "unknown_scope",
    ],
)
async def test_unavailable_catalog_pauses_existing_manual_lot(
    session: AsyncSession,
    unavailable_catalog: str,
):
    tier, duration, scope = await _seed_catalog(session)
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
        title_ru="T",
        title_en="T",
        status="active",
        auto_created=False,
        funpay_id="603",
    )
    session.add(lot)
    await session.flush()

    actions = await LotAutoManager(55).run(session, FakeChatGateway())

    assert any(action.action == "pause" for action in actions)
    assert lot.status == "paused"
    assert lot.paused_reason == "catalog_unavailable"


async def test_unsupported_sellable_tier_is_not_auto_published(
    session: AsyncSession,
):
    tier, duration, scope = await _seed_catalog(session)
    tier.code = "enterprise"
    await _add_account_with_limits(session, tier.id)
    matrix = PriceMatrix(
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
    )
    session.add(matrix)
    await session.flush()
    gateway = FakeChatGateway()

    actions = await LotAutoManager(55).run(session, gateway)

    assert actions == []
    assert list((await session.execute(select(Lot))).scalars()) == []
    assert gateway.saved_offers == {}


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
    assert "Перед выдачей бот проверяет актуальный остаток лимита" in (
        lot.description_ru
    )


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
        ("free", 80, 30 * 24 * 60 * 60, None, None, 30, 90, True),
        ("plus", 20, 5 * 60 * 60, 80, 7 * 24 * 60 * 60, 30, 90, True),
        ("plus", 20, 5 * 60 * 60, 80, 7 * 24 * 60 * 60, 10, 90, True),
    ],
)
async def test_any_capacity_uses_only_verified_long_window(
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
    if has_capacity and max_5h is not None:
        lot = (await session.execute(select(Lot))).scalar_one()
        assert lot.max_5h_pct is None
        assert f"≤ {max_5h}%" not in lot.title_ru
        assert f"≤ {max_5h}%" not in lot.description_ru
