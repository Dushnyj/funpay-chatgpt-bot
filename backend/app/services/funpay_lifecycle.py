from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.integrations.funpay.runner import RunnerCallbacks
from app.integrations.funpay.types import MessageInfo
from app.services.command_router import CommandRouter, UnhandledMessage
from app.services.order_processor import OrderProcessor, LotNotFoundError

logger = logging.getLogger(__name__)


SessionFactory = Callable[[], AsyncSession]


def build_callbacks(
    session_factory: SessionFactory,
    gateway: ChatGateway,
    command_router: CommandRouter | None = None,
) -> RunnerCallbacks:
    """Сборка RunnerCallbacks из сервисов Фазы 3.

    session_factory: возвращает AsyncSession для обработки события.
    gateway: ChatGateway для вызовов FunPay.
    command_router: optional — если None, создаётся пустой (команды не обрабатываются).

    Фаза 4 расширит это: добавит AccountPool, RentalService, KickService callback'и.
    """
    order_processor = OrderProcessor()
    router = command_router or CommandRouter()

    async def on_new_sale(order_id: str) -> None:
        async with session_factory() as session:
            try:
                await order_processor.process_new_sale(session, gateway, order_id)
                await session.commit()
            except LotNotFoundError:
                logger.warning("New sale %s: no matching lot", order_id)
            except Exception:
                logger.exception("Failed to process new sale %s", order_id)

    async def on_sale_closed(order_id: str) -> None:
        async with session_factory() as session:
            try:
                await order_processor.process_sale_closed(session, order_id)
                await session.commit()
            except Exception:
                logger.exception("Failed to process sale closed %s", order_id)

    async def on_sale_refunded(order_id: str) -> None:
        async with session_factory() as session:
            try:
                await order_processor.process_sale_refunded(session, order_id)
                await session.commit()
            except Exception:
                logger.exception("Failed to process sale refunded %s", order_id)

    async def on_message(msg: MessageInfo) -> None:
        async with session_factory() as session:
            ctx = router.build_context(
                chat_id=msg.chat_id,
                sender_id=msg.sender_id or 0,
                text=msg.text or "",
                order_id=msg.order_id,
                lang="ru",
                gateway=gateway,
            )
            try:
                await router.dispatch(ctx)
                await session.commit()
            except UnhandledMessage:
                logger.debug("Unhandled command in chat %s", msg.chat_id)
            except Exception:
                logger.exception("Failed to process message in chat %s", msg.chat_id)

    return RunnerCallbacks(
        on_new_sale=on_new_sale,
        on_sale_closed=on_sale_closed,
        on_sale_refunded=on_sale_refunded,
        on_message=on_message,
    )
