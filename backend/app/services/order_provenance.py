from __future__ import annotations

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.funpay_sale import FunPaySale
from app.models.lot import Lot
from app.models.rental import Order


def exact_lot_binding_exists(order=Order):
    """Correlated proof that an Order snapshot matches its published bot Lot.

    ``lot_id`` alone is intentionally insufficient: old versions could infer a
    lot from seller-wide title/price data.  A managed order must retain either
    the exact remote offer id or the bot-only description marker observed on
    the immutable FunPay order page.
    """

    return exists().where(
        and_(
            Lot.id == order.lot_id,
            or_(
                and_(
                    order.lot_binding_method == "offer_id",
                    order.funpay_offer_id.is_not(None),
                    Lot.funpay_id.is_not(None),
                    order.funpay_offer_id == Lot.funpay_id,
                ),
                and_(
                    order.lot_binding_method == "provenance_token",
                    order.lot_provenance_token.is_not(None),
                    Lot.provenance_token.is_not(None),
                    Lot.provenance_marker_synced.is_(True),
                    order.lot_provenance_token == Lot.provenance_token,
                ),
            ),
        )
    )


def managed_sale_order_exists(
    sale=FunPaySale,
    *,
    allow_pending_chat: bool = False,
):
    """Correlated proof that a sale points to an exactly bound bot order.

    A sale with no chat id may be admitted only to the bounded detail queue (or
    shown as another order of an already verified buyer).  Chat access and all
    secret-disclosure paths use the default strict identity match.
    """

    chat_identity = Order.funpay_chat_id == sale.funpay_chat_id
    if allow_pending_chat:
        chat_identity = or_(chat_identity, sale.funpay_chat_id.is_(None))

    return exists().where(
        and_(
            Order.id == sale.order_id,
            Order.funpay_order_id == sale.funpay_order_id,
            Order.buyer_funpay_id == sale.buyer_funpay_id,
            chat_identity,
            exact_lot_binding_exists(Order),
        )
    )


def verified_sale_for_order_exists(order=Order):
    """Correlated exact sale identity for an already selected Order."""

    return exists().where(
        and_(
            FunPaySale.order_id == order.id,
            FunPaySale.funpay_order_id == order.funpay_order_id,
            FunPaySale.buyer_funpay_id == order.buyer_funpay_id,
            FunPaySale.funpay_chat_id == order.funpay_chat_id,
        )
    )


async def is_verified_bot_sale_order(
    session: AsyncSession,
    order: Order,
) -> bool:
    """Final transactional guard for fulfillment and secret disclosure."""

    if order.id is None:
        return False
    candidate = aliased(Order)
    proof = await session.scalar(
        select(candidate.id).where(
            candidate.id == order.id,
            exact_lot_binding_exists(candidate),
            verified_sale_for_order_exists(candidate),
        )
    )
    return proof is not None


async def is_exactly_bound_order(
    session: AsyncSession,
    order: Order,
) -> bool:
    """Check the immutable lot snapshot without requiring a sale row yet."""

    if order.id is None:
        return False
    candidate = aliased(Order)
    proof = await session.scalar(
        select(candidate.id).where(
            candidate.id == order.id,
            exact_lot_binding_exists(candidate),
        )
    )
    return proof is not None
