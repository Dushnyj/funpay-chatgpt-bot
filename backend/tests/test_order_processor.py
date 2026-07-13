import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import OrderInfo, SaleStatus
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.account import Account, AccountCheckJob
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
    ))
    return gw


async def _seed_catalog_and_lot(session: AsyncSession, funpay_node_id: int = 55) -> int:
    """Создаёт tier+duration+scope+lot и возвращает lot_id."""
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    lot = Lot(
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


async def test_process_new_sale_creates_order(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    order = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert order.funpay_order_id == "ord-1"
    assert order.funpay_chat_id == "100"
    assert order.buyer_funpay_id == "200"
    assert order.lot_id is not None
    assert order.status == "pending"


async def test_process_new_sale_idempotent(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    first = await proc.process_new_sale(session, gateway, order_id="ord-1")
    second = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert first.id == second.id
    result = await session.execute(select(Order).where(Order.funpay_order_id == "ord-1"))
    assert len(result.scalars().all()) == 1


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


async def test_process_new_sale_requires_matching_title_and_price_fallback(
    session: AsyncSession, gateway: FakeChatGateway,
):
    first_id = await _seed_catalog_and_lot(session)
    first = await session.get(Lot, first_id)
    second = Lot(
        funpay_node_id=55, tier_id=first.tier_id, duration_id=first.duration_id,
        limit_scope_id=first.limit_scope_id, price=699,
        title_ru="Other", title_en="Other", status="active", auto_created=False,
        config_key="price-fallback-second",
    )
    session.add(second)
    await session.flush()
    gateway.set_order(OrderInfo(
        order_id="ord-price", status=SaleStatus.PAID, chat_id=100,
        buyer_id=200, subcategory_id=55, title="Other", price=699,
    ))

    order = await OrderProcessor().process_new_sale(session, gateway, "ord-price")

    assert order.lot_id == second.id


async def test_process_new_sale_rejects_ambiguous_multi_lot_fallback(
    session: AsyncSession, gateway: FakeChatGateway,
):
    first_id = await _seed_catalog_and_lot(session)
    first = await session.get(Lot, first_id)
    session.add(Lot(
        funpay_node_id=55, tier_id=first.tier_id, duration_id=first.duration_id,
        limit_scope_id=first.limit_scope_id, price=599,
        title_ru="Plus 7d", title_en="Plus 7d", status="active",
        auto_created=False, config_key="ambiguous-second",
    ))
    await session.flush()

    with pytest.raises(LotNotFoundError):
        await OrderProcessor().process_new_sale(session, gateway, "ord-1")


async def test_refund_retries_revoke_before_releasing_active_rental(
    session: AsyncSession, gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    kick = FakeKickService(success=False)
    processor = OrderProcessor(kick_service=kick)
    order = await processor.process_new_sale(session, gateway, "ord-1")
    account = Account(
        login="refund@example.com", password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP", tier_id=order.tier_id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(account)
    await session.flush()
    rental = Rental(
        order_id=order.id, account_id=account.id,
        buyer_funpay_id=order.buyer_funpay_id,
        buyer_funpay_chat_id=order.funpay_chat_id,
        tier_id=order.tier_id, duration_id=order.duration_id,
        limit_scope_id=order.limit_scope_id, lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
    )
    session.add(rental)
    await session.flush()

    pending = await processor.process_sale_refunded(session, "ord-1")

    assert pending.status == "refund_pending"
    assert rental.status == "active"
    assert account.status == "maintenance"

    kick.success = True
    refunded = await processor.process_sale_refunded(session, "ord-1")

    assert refunded.status == "refunded"
    assert rental.status == "refunded"
    job = (await session.execute(
        select(AccountCheckJob).where(AccountCheckJob.account_id == account.id)
    )).scalar_one()
    assert job.job_type == "refresh_recover"
