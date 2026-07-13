import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import OrderInfo, SaleStatus
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.account import Account, AccountCheckJob
from app.models.audit import AuditLog
from app.models.lot import Lot
from app.models.rental import Order, Rental
from app.services.kick_service import KickResult
from app.services.order_processor import OrderProcessor, LotNotFoundError


class FakeKickService:
    def __init__(self, success: bool):
        self.success = success
        self.calls: list[int] = []

    async def kick(self, _session, account_id: int) -> KickResult:
        self.calls.append(account_id)
        return KickResult(
            success=self.success,
            error=None if self.success else "temporary browser failure",
        )


@pytest.fixture
def gateway() -> FakeChatGateway:
    gw = FakeChatGateway()
    gw.set_order(OrderInfo(
        order_id="ord-1",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="Plus 7d",
        price=599.0,
        offer_id=9001,
    ))
    return gw


async def _seed_catalog_and_lot(session: AsyncSession, funpay_node_id: int = 55) -> int:
    """Создаёт tier+duration+scope+lot и возвращает lot_id."""
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    lot = Lot(
        funpay_id="9001",
        funpay_node_id=funpay_node_id,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Plus 7d",
        title_en="Plus 7d",
        status="active",
        auto_created=True,
    )
    session.add(lot)
    await session.flush()
    return lot.id


async def _add_active_rental(
    session: AsyncSession,
    order: Order,
    *,
    login: str = "refund@example.com",
) -> tuple[Rental, Account]:
    account = Account(
        login=login,
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=order.tier_id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(account)
    await session.flush()
    rental = Rental(
        order_id=order.id,
        account_id=account.id,
        buyer_funpay_id=order.buyer_funpay_id,
        buyer_funpay_chat_id=order.funpay_chat_id,
        tier_id=order.tier_id,
        duration_id=order.duration_id,
        limit_scope_id=order.limit_scope_id,
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
    )
    session.add(rental)
    await session.flush()
    return rental, account


async def test_process_new_sale_creates_order(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    order = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert order.funpay_order_id == "ord-1"
    assert order.funpay_chat_id == "100"
    assert order.buyer_funpay_id == "200"
    assert order.lot_id is not None
    assert order.lot_binding_method == "offer_id"
    assert order.funpay_offer_id == "9001"
    assert order.lot_provenance_token is None
    assert order.status == "pending"


async def test_process_new_sale_idempotent(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    first = await proc.process_new_sale(session, gateway, order_id="ord-1")
    second = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert first.id == second.id
    result = await session.execute(select(Order).where(Order.funpay_order_id == "ord-1"))
    assert len(result.scalars().all()) == 1


async def test_existing_legacy_order_is_not_promoted_without_snapshot(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    lot_id = await _seed_catalog_and_lot(session)
    legacy = Order(
        funpay_order_id="legacy-order",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        lot_id=lot_id,
        price=599,
        status="pending",
    )
    session.add(legacy)
    await session.flush()

    returned = await OrderProcessor().process_new_sale(
        session,
        gateway,
        "legacy-order",
        info=OrderInfo(
            order_id="legacy-order",
            status=SaleStatus.PAID,
            chat_id=100,
            buyer_id=200,
            subcategory_id=55,
            title="Plus 7d",
            price=599,
            offer_id=9001,
        ),
    )

    assert returned.id == legacy.id
    assert returned.lot_binding_method is None
    assert returned.funpay_offer_id is None
    assert returned.lot_provenance_token is None


async def test_process_new_sale_rejects_unmatched_sole_subcategory_lot(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    gateway.set_order(OrderInfo(
        order_id="ord-mismatch",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="Completely different offer",
        price=1.0,
    ))

    with pytest.raises(LotNotFoundError):
        await OrderProcessor().process_new_sale(
            session,
            gateway,
            order_id="ord-mismatch",
        )


async def test_process_new_sale_no_matching_lot_raises(
    session: AsyncSession, gateway: FakeChatGateway,
):
    proc = OrderProcessor()
    with pytest.raises(LotNotFoundError):
        await proc.process_new_sale(session, gateway, order_id="ord-1")


async def test_process_sale_closed_marks_completed(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    await proc.process_new_sale(session, gateway, order_id="ord-1")
    order = await proc.process_sale_closed(session, order_id="ord-1")
    assert order.status == "completed"


async def test_process_sale_refunded_marks_refunded(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    await proc.process_new_sale(session, gateway, order_id="ord-1")
    order = await proc.process_sale_refunded(session, order_id="ord-1")
    assert order.status == "refunded"


async def test_late_refund_after_expiry_keeps_history_consistent(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    order = await OrderProcessor().process_new_sale(session, gateway, "ord-1")
    rental, _account = await _add_active_rental(session, order)
    rental.status = "expired"
    await session.commit()
    kick = FakeKickService(success=True)

    refunded = await OrderProcessor(
        kick_service=kick,
    ).process_sale_refunded(session, "ord-1")

    await session.refresh(rental)
    assert refunded.status == "refunded"
    assert rental.status == "refunded"
    assert kick.calls == []
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "late_refund_terminal_rental"
            )
        )
    ).scalar_one()
    assert audit.metadata_["previous_status"] == "expired"


async def test_refund_releases_stale_replacement_target(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    order = await OrderProcessor().process_new_sale(session, gateway, "ord-1")
    rental, _old_account = await _add_active_rental(session, order)
    target = Account(
        login="stale-target@example.com",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=order.tier_id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(target)
    await session.flush()
    rental.replacement_target_account_id = target.id
    rental.expiry_revoke_started_at = (
        datetime.now(timezone.utc) - timedelta(minutes=6)
    )
    await session.commit()

    refunded = await OrderProcessor(
        kick_service=FakeKickService(success=True),
    ).process_sale_refunded(session, "ord-1")

    await session.refresh(rental)
    assert refunded.status == "refunded"
    assert rental.replacement_target_account_id is None


async def test_late_close_cannot_resurrect_refunded_order(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    await proc.process_new_sale(session, gateway, order_id="ord-1")
    await proc.process_sale_refunded(session, order_id="ord-1")

    order = await proc.process_sale_closed(session, order_id="ord-1")

    assert order.status == "refunded"


async def test_late_close_cannot_cancel_pending_refund_revoke(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    order = await proc.process_new_sale(session, gateway, order_id="ord-1")
    order.status = "refund_pending"
    await session.flush()

    closed = await proc.process_sale_closed(session, order_id="ord-1")

    assert closed.status == "refund_pending"


async def test_process_sale_closed_unknown_order_raises(session: AsyncSession):
    proc = OrderProcessor()
    with pytest.raises(KeyError):
        await proc.process_sale_closed(session, order_id="nope")


async def test_process_new_sale_records_tier_duration_scope(
    session: AsyncSession, gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    order = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert order.tier_id is not None
    assert order.duration_id is not None
    assert order.limit_scope_id is not None


async def test_process_new_sale_records_english_offer_locale(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    lot_id = await _seed_catalog_and_lot(session)
    lot = await session.get(Lot, lot_id)
    assert lot is not None
    lot.title_ru = "Plus на 7 дней"
    lot.title_en = "Plus for 7 days"
    gateway.set_order(OrderInfo(
        order_id="ord-en",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="Plus for 7 days",
        price=599.0,
        offer_id=9001,
    ))

    order = await OrderProcessor().process_new_sale(session, gateway, "ord-en")

    assert order.buyer_locale == "en"


async def test_process_new_sale_prefers_remote_offer_id(
    session: AsyncSession, gateway: FakeChatGateway,
):
    first_id = await _seed_catalog_and_lot(session)
    first = await session.get(Lot, first_id)
    first.funpay_id = "101"
    second = Lot(
        funpay_id="202", funpay_node_id=55,
        tier_id=first.tier_id, duration_id=first.duration_id,
        limit_scope_id=first.limit_scope_id, price=699,
        title_ru="Other", title_en="Other", status="active", auto_created=False,
        config_key="remote-offer-second",
    )
    session.add(second)
    await session.flush()
    gateway.set_order(OrderInfo(
        order_id="ord-remote", status=SaleStatus.PAID, chat_id=100,
        buyer_id=200, subcategory_id=55, title="ambiguous", price=599,
        offer_id=202,
    ))

    order = await OrderProcessor().process_new_sale(session, gateway, "ord-remote")

    assert order.lot_id == second.id
    assert order.lot_binding_method == "offer_id"
    assert order.funpay_offer_id == "202"
    assert order.lot_provenance_token is None


async def test_process_new_sale_accepts_exact_provenance_marker(
    session: AsyncSession, gateway: FakeChatGateway,
):
    first_id = await _seed_catalog_and_lot(session)
    first = await session.get(Lot, first_id)
    second = Lot(
        funpay_id="9002", funpay_node_id=55,
        tier_id=first.tier_id, duration_id=first.duration_id,
        limit_scope_id=first.limit_scope_id, price=699,
        title_ru="Other", title_en="Other", status="active", auto_created=False,
        config_key="price-fallback-second",
        provenance_marker_synced=True,
    )
    session.add(second)
    await session.flush()
    gateway.set_order(OrderInfo(
        order_id="ord-token", status=SaleStatus.PAID, chat_id=100,
        buyer_id=200, subcategory_id=999, title="Unrelated display title", price=1,
        full_description=(
            f"Remote description\n\n[FPBOT:{second.provenance_token}]"
        ),
    ))

    order = await OrderProcessor().process_new_sale(session, gateway, "ord-token")

    assert order.lot_id == second.id
    assert order.lot_binding_method == "provenance_token"
    assert order.funpay_offer_id is None
    assert order.lot_provenance_token == second.provenance_token


async def test_title_category_and_price_never_authorize_a_sale(
    session: AsyncSession, gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    gateway.set_order(OrderInfo(
        order_id="display-only",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="Plus 7d",
        price=599,
        full_description="Same visible description, but no bot marker",
    ))

    with pytest.raises(LotNotFoundError):
        await OrderProcessor().process_new_sale(session, gateway, "display-only")


@pytest.mark.parametrize(
    ("funpay_id", "marker_synced"),
    [
        (None, True),
        ("9001", False),
    ],
)
async def test_provenance_marker_requires_published_synced_lot(
    session: AsyncSession,
    gateway: FakeChatGateway,
    funpay_id: str | None,
    marker_synced: bool,
):
    lot_id = await _seed_catalog_and_lot(session)
    lot = await session.get(Lot, lot_id)
    assert lot is not None
    lot.funpay_id = funpay_id
    lot.provenance_marker_synced = marker_synced
    await session.flush()
    gateway.set_order(OrderInfo(
        order_id="untrusted-token",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="Anything",
        price=1,
        full_description=f"[FPBOT:{lot.provenance_token}]",
    ))

    with pytest.raises(LotNotFoundError):
        await OrderProcessor().process_new_sale(
            session,
            gateway,
            "untrusted-token",
        )

    assert await session.scalar(
        select(Order).where(Order.funpay_order_id == "untrusted-token")
    ) is None


async def test_duplicate_provenance_markers_fail_closed(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    lot_id = await _seed_catalog_and_lot(session)
    lot = await session.get(Lot, lot_id)
    assert lot is not None
    lot.provenance_marker_synced = True
    await session.flush()
    marker = f"[FPBOT:{lot.provenance_token}]"
    gateway.set_order(OrderInfo(
        order_id="duplicate-marker",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="Anything",
        price=1,
        full_description=f"{marker}\n{marker}",
    ))

    with pytest.raises(LotNotFoundError):
        await OrderProcessor().process_new_sale(session, gateway, "duplicate-marker")


async def test_conflicting_offer_id_and_marker_fail_closed(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    first_id = await _seed_catalog_and_lot(session)
    first = await session.get(Lot, first_id)
    assert first is not None
    second = Lot(
        funpay_id="9002",
        funpay_node_id=55,
        tier_id=first.tier_id,
        duration_id=first.duration_id,
        limit_scope_id=first.limit_scope_id,
        price=699,
        title_ru="Other",
        title_en="Other",
        status="active",
        auto_created=False,
        config_key="conflicting-proof-second",
        provenance_marker_synced=True,
    )
    session.add(second)
    await session.flush()
    gateway.set_order(OrderInfo(
        order_id="conflicting-proof",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="Anything",
        price=1,
        offer_id=9001,
        full_description=f"[FPBOT:{second.provenance_token}]",
    ))

    with pytest.raises(LotNotFoundError):
        await OrderProcessor().process_new_sale(session, gateway, "conflicting-proof")


@pytest.mark.parametrize("status", ["paused", "deleted"])
async def test_process_new_sale_accepts_delayed_event_for_published_lot(
    session: AsyncSession, gateway: FakeChatGateway, status: str,
):
    lot_id = await _seed_catalog_and_lot(session)
    lot = await session.get(Lot, lot_id)
    assert lot is not None
    lot.status = status
    await session.flush()

    order = await OrderProcessor().process_new_sale(session, gateway, "ord-1")

    assert order.lot_id == lot_id
    assert order.lot_binding_method == "offer_id"
    assert order.funpay_offer_id == "9001"


async def test_refund_retries_revoke_before_releasing_active_rental(
    session: AsyncSession, gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    kick = FakeKickService(success=False)
    processor = OrderProcessor(kick_service=kick)
    order = await processor.process_new_sale(session, gateway, "ord-1")
    rental, account = await _add_active_rental(session, order)

    pending = await processor.process_sale_refunded(session, "ord-1")

    assert pending.status == "refund_pending"
    assert rental.status == "active"
    assert account.status == "maintenance"
    assert rental.expiry_revoke_started_at is None

    kick.success = True
    refunded = await processor.process_sale_refunded(session, "ord-1")

    assert refunded.status == "refunded"
    assert rental.status == "refunded"
    job = (await session.execute(
        select(AccountCheckJob).where(AccountCheckJob.account_id == account.id)
    )).scalar_one()
    assert job.job_type == "refresh_recover"


async def test_duplicate_refund_during_kick_does_not_start_second_kick(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)

    class DuplicateDuringKick:
        def __init__(self):
            self.processor: OrderProcessor | None = None
            self.calls: list[int] = []
            self.duplicate_status: str | None = None

        async def kick(self, db: AsyncSession, account_id: int) -> KickResult:
            assert not db.in_transaction()
            self.calls.append(account_id)
            assert self.processor is not None
            duplicate = await self.processor.process_sale_refunded(db, "ord-1")
            self.duplicate_status = duplicate.status
            return KickResult(success=True)

    kick = DuplicateDuringKick()
    processor = OrderProcessor(kick_service=kick)
    kick.processor = processor
    order = await processor.process_new_sale(session, gateway, "ord-1")
    rental, account = await _add_active_rental(session, order)

    refunded = await processor.process_sale_refunded(session, "ord-1")

    await session.refresh(rental)
    assert kick.calls == [account.id]
    assert kick.duplicate_status == "refund_pending"
    assert refunded.status == "refunded"
    assert rental.status == "refunded"


async def test_late_close_during_refund_kick_cannot_cancel_refund(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)

    class CloseDuringKick:
        def __init__(self):
            self.processor: OrderProcessor | None = None
            self.close_status: str | None = None

        async def kick(self, db: AsyncSession, _account_id: int) -> KickResult:
            assert self.processor is not None
            closed = await self.processor.process_sale_closed(db, "ord-1")
            self.close_status = closed.status
            return KickResult(success=True)

    kick = CloseDuringKick()
    processor = OrderProcessor(kick_service=kick)
    kick.processor = processor
    order = await processor.process_new_sale(session, gateway, "ord-1")
    rental, _account = await _add_active_rental(session, order)

    refunded = await processor.process_sale_refunded(session, "ord-1")

    await session.refresh(rental)
    assert kick.close_status == "refund_pending"
    assert refunded.status == "refunded"
    assert rental.status == "refunded"


async def test_refund_success_does_not_finalize_a_changed_account(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    order = await OrderProcessor().process_new_sale(session, gateway, "ord-1")
    rental, original_account = await _add_active_rental(session, order)

    class ChangeAccountDuringKick:
        async def kick(self, db: AsyncSession, _account_id: int) -> KickResult:
            replacement = Account(
                login="replacement@example.com",
                password_encrypted="pass",
                totp_secret_encrypted="JBSWY3DPEHPK3PXP",
                tier_id=order.tier_id,
                status="active",
                subscription_expires_at=(
                    datetime.now(timezone.utc) + timedelta(days=30)
                ),
            )
            db.add(replacement)
            await db.flush()
            current = await db.get(Rental, rental.id)
            assert current is not None
            current.account_id = replacement.id
            await db.commit()
            return KickResult(success=True)

    processor = OrderProcessor(kick_service=ChangeAccountDuringKick())
    pending = await processor.process_sale_refunded(session, "ord-1")

    await session.refresh(rental)
    await session.refresh(original_account)
    assert pending.status == "refund_pending"
    assert rental.status == "active"
    assert rental.account_id != original_account.id
    assert rental.expiry_revoke_started_at is None
    assert original_account.status == "maintenance"
    assert (await session.execute(select(AccountCheckJob))).scalars().all() == []


async def test_late_refund_completion_cannot_close_newer_revoke_claim(
    session: AsyncSession,
    gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    order = await OrderProcessor().process_new_sale(session, gateway, "ord-1")
    rental, _account = await _add_active_rental(session, order)

    class ReplaceClaimDuringKick:
        newer_claim: datetime | None = None

        async def kick(self, db: AsyncSession, _account_id: int) -> KickResult:
            current = await db.get(Rental, rental.id)
            assert current is not None
            self.newer_claim = datetime.now(timezone.utc) + timedelta(seconds=1)
            current.expiry_revoke_started_at = self.newer_claim
            await db.commit()
            return KickResult(success=True)

    kick = ReplaceClaimDuringKick()
    pending = await OrderProcessor(kick_service=kick).process_sale_refunded(
        session, "ord-1",
    )

    await session.refresh(rental)
    stored_claim = rental.expiry_revoke_started_at
    if stored_claim is not None and stored_claim.tzinfo is None:
        stored_claim = stored_claim.replace(tzinfo=timezone.utc)
    assert pending.status == "refund_pending"
    assert rental.status == "active"
    assert stored_claim == kick.newer_claim
    assert (await session.execute(select(AccountCheckJob))).scalars().all() == []
