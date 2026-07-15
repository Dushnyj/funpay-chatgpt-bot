import pytest
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.provenance import (
    exact_provenance_token,
    public_provenance_code,
)
from app.integrations.funpay.types import OfferInfo
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot
from app.models.message import MessageTemplate
from app.services.lot_sync import (
    description_with_provenance_marker,
    build_offer_fields,
    LotSyncService,
    LotNotPublishedError,
    provenance_marker,
)


_PAYMENT_MESSAGE_RU = "✅ Оплата получена. Данные придут в этот чат."
_PAYMENT_MESSAGE_EN = "✅ Payment received. Access details will arrive here."


async def _make_lot(session: AsyncSession) -> Lot:
    tier = SubscriptionTier(code="plus", name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    session.add_all([
        MessageTemplate(
            key="payment_received",
            lang="ru",
            content=_PAYMENT_MESSAGE_RU,
        ),
        MessageTemplate(
            key="payment_received",
            lang="en",
            content=_PAYMENT_MESSAGE_EN,
        ),
    ])
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


def _build_fields(
    lot: Lot,
    tier: SubscriptionTier,
    *,
    offer_id: int,
    active: bool,
):
    return build_offer_fields(
        lot,
        tier,
        offer_id=offer_id,
        active=active,
        payment_msg_ru=_PAYMENT_MESSAGE_RU,
        payment_msg_en=_PAYMENT_MESSAGE_EN,
    )


async def test_build_offer_fields_new_lot(session: AsyncSession):
    lot = await _make_lot(session)
    tier = await session.get(SubscriptionTier, lot.tier_id)
    assert tier is not None
    fields = _build_fields(lot, tier, offer_id=0, active=True)
    marker_ru = provenance_marker(lot.provenance_token, lang="ru")
    marker_en = provenance_marker(lot.provenance_token, lang="en")
    assert fields.offer_id == 0
    assert fields.subcategory_id == 55
    assert fields.title_ru == "Plus 7 дней"
    assert fields.title_en == "Plus 7 days"
    assert fields.desc_ru == f"Описание лота\n\n{marker_ru}"
    assert fields.desc_en == f"Lot description\n\n{marker_en}"
    assert fields.payment_msg_ru == _PAYMENT_MESSAGE_RU
    assert fields.payment_msg_en == _PAYMENT_MESSAGE_EN
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
    marker_ru = provenance_marker(lot.provenance_token, lang="ru")
    marker_en = provenance_marker(lot.provenance_token, lang="en")
    lot.description_ru = f"Описание\n\n{marker_ru}\n\n{marker_ru}"
    lot.description_en = "X" * 5000
    tier = await session.get(SubscriptionTier, lot.tier_id)
    assert tier is not None

    first = _build_fields(lot, tier, offer_id=0, active=True)
    second = _build_fields(lot, tier, offer_id=0, active=True)

    assert first.desc_ru == second.desc_ru == f"Описание\n\n{marker_ru}"
    assert first.desc_ru.count(marker_ru) == 1
    assert first.desc_en.count(marker_en) == 1
    assert first.desc_en.endswith(marker_en)
    assert len(first.desc_en) <= 4000
    assert description_with_provenance_marker(first.desc_ru, lot.provenance_token) == (
        first.desc_ru
    )


async def test_readable_marker_preserves_full_identity_and_accepts_legacy(
    session: AsyncSession,
):
    lot = await _make_lot(session)
    token = lot.provenance_token
    public_code = public_provenance_code(token)
    marker_ru = provenance_marker(token, lang="ru")
    marker_en = provenance_marker(token, lang="en")
    legacy = f"[FPBOT:{token}]"

    assert public_code in marker_ru
    assert public_code in marker_en
    assert exact_provenance_token((marker_ru, marker_en)) == token
    assert exact_provenance_token((legacy,)) == token
    assert exact_provenance_token((legacy, marker_en)) == token
    assert exact_provenance_token((f"{legacy}\n{marker_ru}",)) is None
    extended_code = f"{public_code}A"
    assert exact_provenance_token((extended_code,)) is None

    migrated = description_with_provenance_marker(
        f"Описание\n\n{legacy}",
        token,
        lang="ru",
    )
    assert migrated == f"Описание\n\n{marker_ru}"
    assert "[FPBOT:" not in migrated

    malformed_preserved = description_with_provenance_marker(
        f"Описание\n\n{extended_code}",
        token,
        lang="ru",
    )
    assert extended_code in malformed_preserved
    assert malformed_preserved.endswith(marker_ru)


async def test_build_offer_fields_existing_lot(session: AsyncSession):
    lot = await _make_lot(session)
    tier = await session.get(SubscriptionTier, lot.tier_id)
    assert tier is not None
    fields = _build_fields(lot, tier, offer_id=42, active=False)
    assert fields.offer_id == 42
    assert fields.active is False


async def test_build_offer_fields_uses_node_id_as_subcategory(session: AsyncSession):
    lot = await _make_lot(session)
    tier = await session.get(SubscriptionTier, lot.tier_id)
    assert tier is not None
    fields = _build_fields(lot, tier, offer_id=0, active=True)
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
    assert saved.desc_ru.endswith(
        provenance_marker(lot.provenance_token, lang="ru")
    )
    assert saved.payment_msg_ru == _PAYMENT_MESSAGE_RU
    assert saved.payment_msg_en == _PAYMENT_MESSAGE_EN
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
        desc_ru=(
            "Создано ботом\n\n"
            f"{provenance_marker(lot.provenance_token, lang='ru')}"
        ),
        desc_en=(
            "Created by bot\n\n"
            f"{provenance_marker(lot.provenance_token, lang='en')}"
        ),
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
        provenance_marker(lot.provenance_token, lang="ru")
    )
    assert gateway.offer_description_calls == [777]


async def test_sync_rejects_ambiguous_or_mismatched_recovery_marker(
    session: AsyncSession, gateway: FakeChatGateway,
):
    lot = await _make_lot(session)
    await session.commit()
    marker = provenance_marker(lot.provenance_token, lang="ru")
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


async def test_sync_permanently_deletes_exact_token_duplicates(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    lot = await _make_lot(session)
    await session.commit()
    offers = [
        OfferInfo(778, 55, lot.title_ru, float(lot.price), False, False),
        OfferInfo(777, 55, lot.title_ru, float(lot.price), False, False),
    ]
    gateway.set_my_offers(55, offers)
    for offer_id in (777, 778):
        gateway.set_offer_descriptions(
            offer_id,
            desc_ru=provenance_marker(lot.provenance_token, lang="ru"),
            desc_en=provenance_marker(lot.provenance_token, lang="en"),
        )

    funpay_id = await LotSyncService().sync_lot(
        session,
        gateway,
        lot.id,
        active=True,
    )

    assert funpay_id == 777
    assert lot.funpay_id == "777"
    assert gateway.deleted_offers == [778]
    assert [item.offer_id for item in await gateway.get_my_offers(55)] == [777]
    assert set(gateway.saved_offers) == {777}


async def test_sync_fails_closed_when_duplicate_deletion_is_unconfirmed(
    session: AsyncSession,
):
    class RejectingDeleteGateway(FakeChatGateway):
        async def delete_offer(
            self,
            offer_id: int,
            *,
            expected_provenance_token: str,
        ) -> bool:
            return False

    lot = await _make_lot(session)
    await session.commit()
    gateway = RejectingDeleteGateway()
    gateway.set_my_offers(55, [
        OfferInfo(777, 55, lot.title_ru, float(lot.price), False, False),
        OfferInfo(778, 55, lot.title_ru, float(lot.price), False, False),
    ])
    for offer_id in (777, 778):
        gateway.set_offer_descriptions(
            offer_id,
            desc_ru=provenance_marker(lot.provenance_token, lang="ru"),
            desc_en=provenance_marker(lot.provenance_token, lang="en"),
        )

    with pytest.raises(RuntimeError, match="Unable to delete a duplicate"):
        await LotSyncService().sync_lot(
            session,
            gateway,
            lot.id,
            active=True,
        )

    assert lot.funpay_id is None
    assert set(gateway.saved_offers) == {777, 778}


async def test_delete_lot_permanently_removes_bound_offer(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    lot = await _make_lot(session)
    lot.funpay_id = "900"
    lot.provenance_marker_synced = True
    gateway.set_my_offers(55, [
        OfferInfo(900, 55, lot.title_ru, float(lot.price), False, False),
    ])
    gateway.set_offer_descriptions(
        900,
        desc_ru=provenance_marker(lot.provenance_token, lang="ru"),
        desc_en=provenance_marker(lot.provenance_token, lang="en"),
    )

    await LotSyncService().delete_lot(session, gateway, lot.id)

    assert gateway.deleted_offers == [900]
    assert await gateway.get_my_offers(55) == []
    assert lot.status == "deleted"
    assert lot.paused_reason == "manual_deleted"


async def test_delete_lot_does_not_change_local_state_on_marker_mismatch(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    lot = await _make_lot(session)
    lot.funpay_id = "901"
    lot.provenance_marker_synced = True
    gateway.set_my_offers(55, [
        OfferInfo(901, 55, lot.title_ru, float(lot.price), False, False),
    ])
    gateway.set_offer_descriptions(
        901,
        desc_ru="[FPBOT:ffffffffffffffffffffffffffffffff]",
        desc_en="[FPBOT:ffffffffffffffffffffffffffffffff]",
    )

    with pytest.raises(RuntimeError, match="did not confirm deletion"):
        await LotSyncService().delete_lot(session, gateway, lot.id)

    assert gateway.deleted_offers == []
    assert lot.status == "active"


async def test_delete_lot_rejects_unsynced_provenance_without_remote_call(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    lot = await _make_lot(session)
    lot.funpay_id = "902"
    lot.provenance_marker_synced = False
    gateway.set_my_offers(55, [
        OfferInfo(902, 55, lot.title_ru, float(lot.price), False, False),
    ])

    with pytest.raises(RuntimeError, match="without an exact synced provenance"):
        await LotSyncService().delete_lot(session, gateway, lot.id)

    assert gateway.deleted_offers == []
    assert [item.offer_id for item in await gateway.get_my_offers(55)] == [902]
    assert lot.status == "active"


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
        provenance_marker(lot.provenance_token, lang="en")
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
