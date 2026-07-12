from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.integrations.funpay.runner import RunnerCallbacks
from app.integrations.funpay.types import MessageInfo
from app.services.command_handlers import (
    CodeHandler,
    HelpHandler,
    SubscriptionHandler,
    SellerHandler,
    ReplaceHandler,
)
from app.services.command_parser import CommandType
from app.services.command_router import CommandRouter, UnhandledMessage
from app.services.chat_service import ChatService
from app.services.order_processor import OrderProcessor, LotNotFoundError
from app.services.rental_service import RentalService

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
    rental_service = RentalService()
    chat_service = ChatService()
    router = command_router or CommandRouter()

    # Регистрируем хэндлеры команд для on_message.
    router.register(CommandType.CODE, CodeHandler())
    router.register(CommandType.HELP, HelpHandler())
    router.register(CommandType.SUBSCRIPTION, SubscriptionHandler())
    router.register(CommandType.SELLER, SellerHandler())
    router.register(CommandType.REPLACE, ReplaceHandler())

    async def on_new_sale(order_id: str) -> None:
        async with session_factory() as session:
            try:
                order = await order_processor.process_new_sale(
                    session, gateway, order_id,
                )
                # Order создаётся и коммитится отдельно от fulfill_order,
                # чтобы сбой выдачи аккаунта не откатил сам заказ
                # (Order нужен для последующих on_sale_closed/refunded).
                await session.commit()
            except LotNotFoundError:
                logger.warning("New sale %s: no matching lot", order_id)
                return
            except Exception:
                logger.exception("Failed to process new sale %s", order_id)
                return

            try:
                # Выдача аккаунта + welcome сообщение.
                # Если аккаунта нет — RentalService отправит no_account_available
                # и вернёт None (покупатель получит уведомление о повторе).
                from app.models.settings import SellerSettings
                settings = await session.get(SellerSettings, 1)
                max_rentals = (
                    settings.default_max_active_rentals if settings else 1
                )
                await rental_service.fulfill_order(
                    session, gateway, order.id, max_rentals,
                )
                await session.commit()
            except Exception:
                logger.exception(
                    "Failed to fulfill order %s (order record saved)",
                    order_id,
                )

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
            try:
                _, created = await chat_service.record_event(session, msg)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Failed to persist message in chat %s", msg.chat_id)
                created = True

            # Duplicate FunPay events must not increment unread counters or run
            # a buyer command twice.
            if not created:
                return

            # Messages sent by the seller/bot are part of history, but must
            # never be parsed as buyer commands.
            if msg.from_me:
                return

            ctx = router.build_context(
                chat_id=msg.chat_id,
                sender_id=msg.sender_id or 0,
                text=msg.text or "",
                order_id=msg.order_id,
                lang="ru",
                gateway=gateway,
            )
            # Передаём session в контекст для хэндлеров команд
            # (CommandContext — frozen dataclass, поэтому через object.__setattr__).
            object.__setattr__(ctx, "_session", session)
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
