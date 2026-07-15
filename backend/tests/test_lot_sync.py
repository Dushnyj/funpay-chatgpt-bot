import pytest
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import OfferInfo
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot
from app.services.lot_sync import (
    description_with_provenance_marker,
    build_offer_fields,
    LotSyncService,
    LotNotPublishedError,
    provenance_marker,
)


async def _make_lot(session: AsyncSession) -> Lot:
    tier = SubscriptionTier(code="plus", name="Plus", is_active=True)
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
    tier = await session.get(SubscriptionTier, lot.tier_id)
    assert tier is not None
    fields = build_offer_fields(lot, tier, offer_id=0, active=True)
    marker = provenance_marker(lot.provenance_token)
    assert fields.offer_id == 0
    assert fields.subcategory_id == 55
    assert fields.title_ru == "Plus 7 дней"
    assert fields.title_en == "Plus 7 days"
    assert fields.desc_ru == f"Описание лота\n\n{marker}"
    assert fields.desc_en == f"Lot description\n\n{marker}"
    assert fields.payment_msg_ru.startswith("Заказ принят.")
    assert fields.payment_msg_en.startswith("Order accepted.")
    assert fields.subscription.value == "С подпиской"
    assert fields.subscription_type is not None
    assert fields.subscription_type.value == "Plus"
    assert fields.price == 599.0
    assert fields.amount == 1
    assert fields.active is True
    assert fields.auto_delivery is False
    assert lot.provenance_marker_synced is False


async def test_description_marker_is_stable_deduplicated_and_bounded(
    session: AsyncSession,
):
    lot = await _make_lot(session)
    marker = provenance_marker(lot.provenance_token)
    lot.description_ru = f"Описание\n\n{marker}\n\n{marker}"
    lot.description_en = "X" * 5000
    tier = await session.get(SubscriptionTier, lot.tier_id)
    assert tier is not None

    first = build_offer_fields(lot, tier, offer_id=0, active=True)
    second = build_offer_fields(lot, tier, offer_id=0, active=True)

    assert first.desc_ru == second.desc_ru == f"Описание\n\n{marker}"
    assert first.desc_ru.count(marker) == 1
    assert first.desc_en.count(marker) == 1
    assert first.desc_en.endswith(marker)
    assert len(first.desc_en) <= 4000
    assert description_with_provenance_marker(first.desc_ru, lot.provenance_token) == (
        first.desc_ru
    )


async def test_build_offer_fields_existing_lot(session: AsyncSession):
    lot = await _make_lot(session)
    tier = await session.get(SubscriptionTier, lot.tier_id)
    assert tier is not None
    fields = build_offer_fields(lot, tier, offer_id=42, active=False)
    assert fields.offer_id == 42
    assert fields.active is False


async def test_build_offer_fields_uses_node_id_as_subcategory(session: AsyncSession):
    lot = await _make_lot(session)
    tier = await session.get(SubscriptionTier, lot.tier_id)
    assert tier is not None
    fields = build_offer_fields(lot, tier, offer_id=0, active=True)
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
    assert saved.desc_ru.endswith(provenance_marker(lot.provenance_token))
    assert lot.provenance_marker_synced is True


async def test_sync_updates_existing_offer(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = "100"
    await session.flush()
    svc = LotSyncService()
    funpay_id = await svc.sync_lot(session, gateway, lot.id, active=False)
    assert funpay_id == 100
    assert gateway.saved_offers[100].active is False
    assert lot.provenance_marker_synced is True


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
    gateway.set_offer_descriptions(
        777,
        desc_ru=f"Создано ботом\n\n{provenance_marker(lot.provenance_token)}",
        desc_en=f"Created by bot\n\n{provenance_marker(lot.provenance_token)}",
    )

    funpay_id = await LotSyncService().sync_lot(
        session, gateway, lot.id, active=True,
    )

    assert funpay_id == 777
    assert lot.funpay_id == "777"
    assert set(gateway.saved_offers) == {777}


async def test_sync_never_adopts_manual_title_price_lookalike(
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
    ), OfferInfo(
        offer_id=778,
        subcategory_id=55,
        title="Unrelated seller offer",
        price=float(lot.price),
        active=True,
        auto_delivery=False,
    )])
    gateway.set_offer_descriptions(
        777,
        desc_ru="Ручной лот продавца",
        desc_en="Seller's manual offer",
    )

    funpay_id = await LotSyncService().sync_lot(
        session, gateway, lot.id, active=True,
    )

    assert funpay_id != 777
    assert lot.funpay_id == str(funpay_id)
    assert gateway.saved_offers[777].desc_ru == "Ручной лот продавца"
    assert gateway.saved_offers[funpay_id].desc_ru.endswith(
        provenance_marker(lot.provenance_token)
    )
    assert gateway.offer_description_calls == [777]


async def test_sync_rejects_ambiguous_or_mismatched_recovery_marker(
    session: AsyncSession, gateway: FakeChatGateway,
):
    lot = await _make_lot(session)
    await session.commit()
    marker = provenance_marker(lot.provenance_token)
    gateway.set_my_offers(55, [OfferInfo(
        offer_id=777,
        subcategory_id=55,
        title=lot.title_ru,
        price=float(lot.price),
        active=True,
        auto_delivery=False,
    )])
    gateway.set_offer_descriptions(
        777,
        desc_ru=f"{marker}\n{marker}",
        desc_en="[FPBOT:ffffffffffffffffffffffffffffffff]",
    )

    funpay_id = await LotSyncService().sync_lot(
        session, gateway, lot.id, active=True,
    )

    assert funpay_id != 777
    assert gateway.saved_offers[777].desc_ru == f"{marker}\n{marker}"


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
    lot.provenance_marker_synced = True
    await session.flush()
    svc = LotSyncService()
    await svc.activate_lot(session, gateway, lot.id)
    assert (300, True) in gateway.activity_changes


async def test_activate_unsynced_legacy_offer_performs_full_marker_sync(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    lot = await _make_lot(session)
    lot.funpay_id = "301"
    lot.status = "paused"
    lot.provenance_marker_synced = False
    await session.flush()

    await LotSyncService().activate_lot(session, gateway, lot.id)

    assert gateway.activity_changes == []
    assert gateway.saved_offers[301].active is True
    assert gateway.saved_offers[301].desc_en.endswith(
        provenance_marker(lot.provenance_token)
    )
    assert lot.provenance_marker_synced is True
    assert lot.status == "active"


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
