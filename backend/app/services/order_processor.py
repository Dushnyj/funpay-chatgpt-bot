from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.lot import Lot
from app.models.rental import Order


class LotNotFoundError(Exception):
    """Для заказа не найден Lot с matching funpay_node_id."""


class OrderProcessor:
    """Обработка событий заказа: создание, обновление статуса.

    Создание идемпотентно по funpay_order_id. Определяет lot по funpay_node_id.
    НЕ выдаёт аккаунт — это ответственность Фазы 4 (AccountPool).
    """

    async def process_new_sale(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: str,
    ) -> Order:
        existing = await self._find_order(session, order_id)
        if existing is not None:
            return existing

        info = await gateway.get_order(order_id)
        lot = await self._find_lot_by_node(session, info.subcategory_id)
        if lot is None:
            raise LotNotFoundError(
                f"No active Lot for funpay_node_id={info.subcategory_id} "
                f"(order {order_id})"
            )
        order = Order(
            funpay_order_id=info.order_id,
            funpay_chat_id=str(info.chat_id),
            buyer_funpay_id=str(info.buyer_id),
            buyer_locale="ru",
            lot_id=lot.id,
            tier_id=lot.tier_id,
            duration_id=lot.duration_id,
            limit_scope_id=lot.limit_scope_id,
            min_limit_pct=lot.min_limit_pct,
            max_5h_pct=lot.max_5h_pct,
            max_weekly_pct=lot.max_weekly_pct,
            price=lot.price,
            status="pending",
        )
        session.add(order)
        await session.flush()
        return order

    async def process_sale_closed(
        self,
        session: AsyncSession,
        order_id: str,
    ) -> Order:
        order = await self._get_order_or_raise(session, order_id)
        order.status = "completed"
        await session.flush()
        return order

    async def process_sale_refunded(
        self,
        session: AsyncSession,
        order_id: str,
    ) -> Order:
        order = await self._get_order_or_raise(session, order_id)
        order.status = "refunded"
        await session.flush()
        return order

    async def _find_order(self, session: AsyncSession, order_id: str) -> Order | None:
        result = await session.execute(
            select(Order).where(Order.funpay_order_id == order_id)
        )
        return result.scalar_one_or_none()

    async def _get_order_or_raise(self, session: AsyncSession, order_id: str) -> Order:
        order = await self._find_order(session, order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found")
        return order

    async def _find_lot_by_node(
        self,
        session: AsyncSession,
        funpay_node_id: int,
    ) -> Lot | None:
        result = await session.execute(
            select(Lot).where(
                Lot.funpay_node_id == funpay_node_id,
                Lot.status == "active",
            )
        )
        return result.scalar_one_or_none()
