import pytest
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import OfferInfo
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot
from app.services.lot_sync import (
    build_offer_fields,
    LotSyncService,
    LotNotPublishedError,
)


async def _make_lot(session: AsyncSession) -> Lot:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    lot = Lot(
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Plus 7 дней",
        title_en="Plus 7 days",
        description_ru="Описание лота",
        description_en="Lot description",
        status="active",
        auto_created=True,
    )
    session.add(lot)
    await session.flush()
    return lot


async def test_build_offer_fields_new_lot(session: AsyncSession):
    lot = await _make_lot(session)
    fields = build_offer_fields(lot, offer_id=0, active=True)
    assert fields.offer_id == 0
    assert fields.subcategory_id == 55
    assert fields.title_ru == "Plus 7 дней"
    assert fields.title_en == "Plus 7 days"
    assert fields.desc_ru == "Описание лота"
    assert fields.desc_en == "Lot description"
    assert fields.price == 599.0
    assert fields.active is True
    assert fields.auto_delivery is False


async def test_build_offer_fields_existing_lot(session: AsyncSession):
    lot = await _make_lot(session)
    fields = build_offer_fields(lot, offer_id=42, active=False)
    assert fields.offer_id == 42
    assert fields.active is False


async def test_build_offer_fields_uses_node_id_as_subcategory(session: AsyncSession):
    lot = await _make_lot(session)
    fields = build_offer_fields(lot, offer_id=0, active=True)
    assert fields.subcategory_id == lot.funpay_node_id


@pytest.fixture
def gateway() -> FakeChatGateway:
    return FakeChatGateway()


async def test_sync_creates_new_offer(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = None
    await session.flush()
    svc = LotSyncService()
    funpay_id = await svc.sync_lot(session, gateway, lot.id, active=True)
    assert funpay_id  # вернул новый ID
    await session.refresh(lot)
    assert lot.funpay_id == str(funpay_id)
    assert funpay_id in gateway.saved_offers
    saved = gateway.saved_offers[funpay_id]
    assert saved.active is True
    assert saved.title_ru == "Plus 7 дней"


async def test_sync_updates_existing_offer(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = "100"
    await session.flush()
    svc = LotSyncService()
    funpay_id = await svc.sync_lot(session, gateway, lot.id, active=False)
    assert funpay_id == 100
    assert gateway.saved_offers[100].active is False


async def test_sync_recovers_exact_unclaimed_remote_offer_instead_of_duplicating(
    session: AsyncSession, gateway: FakeChatGateway,
):
    lot = await _make_lot(session)
    await session.commit()
    gateway.set_my_offers(55, [OfferInfo(
        offer_id=777,
        subcategory_id=55,
        title=lot.title_ru,
        price=float(lot.price),
        active=True,
        auto_delivery=False,
    )])

    funpay_id = await LotSyncService().sync_lot(
        session, gateway, lot.id, active=True,
    )

    assert funpay_id == 777
    assert lot.funpay_id == "777"
    assert set(gateway.saved_offers) == {777}


async def test_sync_pause_uses_set_offer_active(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = "200"
    await session.flush()
    svc = LotSyncService()
    await svc.pause_lot(session, gateway, lot.id)
    assert (200, False) in gateway.activity_changes


async def test_sync_activate_uses_set_offer_active(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = "300"
    await session.flush()
    svc = LotSyncService()
    await svc.activate_lot(session, gateway, lot.id)
    assert (300, True) in gateway.activity_changes


async def test_pause_lot_without_funpay_id_raises(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = None
    await session.flush()
    svc = LotSyncService()
    with pytest.raises(LotNotPublishedError):
        await svc.pause_lot(session, gateway, lot.id)


async def test_pause_failure_does_not_change_local_status(session: AsyncSession):
    class RejectingGateway(FakeChatGateway):
        async def set_offer_active(self, offer_id: int, active: bool) -> bool:
            self.activity_changes.append((offer_id, active))
            return False

    lot = await _make_lot(session)
    lot.funpay_id = "400"
    await session.flush()

    with pytest.raises(RuntimeError, match="did not pause"):
        await LotSyncService().pause_lot(session, RejectingGateway(), lot.id)

    assert lot.status == "active"
