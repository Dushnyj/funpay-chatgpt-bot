from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.funpay_sale import FunPaySale
from app.models.lot import Lot
from app.models.rental import Order
from app.services.order_provenance import (
    exact_lot_binding_exists,
    is_verified_bot_sale_order,
    managed_sale_order_exists,
)


async def _seed_order(
    session: AsyncSession,
    *,
    method: str | None,
    offer_snapshot: str | None = None,
    token_snapshot: str | None = None,
    marker_synced: bool = True,
) -> tuple[Lot, Order, FunPaySale]:
    lot = Lot(
        funpay_id="9001",
        provenance_token="a" * 32,
        provenance_marker_synced=marker_synced,
        funpay_node_id=55,
        tier_id=1,
        duration_id=1,
        limit_scope_id=1,
        price=100,
        title_ru="Managed",
        title_en="Managed",
        status="active",
        auto_created=False,
        config_key=f"proof-{method}-{marker_synced}",
    )
    session.add(lot)
    await session.flush()
    order = Order(
        funpay_order_id="ORDER001",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        lot_id=lot.id,
        lot_binding_method=method,
        funpay_offer_id=offer_snapshot,
        lot_provenance_token=token_snapshot,
        price=100,
        status="pending",
    )
    session.add(order)
    await session.flush()
    sale = FunPaySale(
        funpay_order_id=order.funpay_order_id,
        order_id=order.id,
        funpay_chat_id=order.funpay_chat_id,
        buyer_funpay_id=order.buyer_funpay_id,
        status="paid",
    )
    session.add(sale)
    await session.flush()
    return lot, order, sale


async def test_exact_offer_snapshot_authorizes_without_description_marker(
    session: AsyncSession,
):
    _, order, sale = await _seed_order(
        session,
        method="offer_id",
        offer_snapshot="9001",
        marker_synced=False,
    )

    assert await is_verified_bot_sale_order(session, order) is True
    assert await session.scalar(
        select(Order.id).where(exact_lot_binding_exists(Order))
    ) == order.id
    assert await session.scalar(
        select(FunPaySale.id).where(managed_sale_order_exists())
    ) == sale.id


async def test_synced_marker_snapshot_authorizes_but_identity_tampering_does_not(
    session: AsyncSession,
):
    _, order, _ = await _seed_order(
        session,
        method="provenance_token",
        token_snapshot="a" * 32,
    )
    assert await is_verified_bot_sale_order(session, order) is True

    order.buyer_funpay_id = "attacker"
    await session.flush()

    assert await is_verified_bot_sale_order(session, order) is False


async def test_legacy_lot_id_and_unsynced_marker_never_authorize(
    session: AsyncSession,
):
    _, legacy, _ = await _seed_order(session, method=None)
    assert await is_verified_bot_sale_order(session, legacy) is False

    # Reuse a clean database identity after removing the first fixture rows.
    await session.rollback()
    _, unsynced, _ = await _seed_order(
        session,
        method="provenance_token",
        token_snapshot="a" * 32,
        marker_synced=False,
    )
    assert await is_verified_bot_sale_order(session, unsynced) is False
