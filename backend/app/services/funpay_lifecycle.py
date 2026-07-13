from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.integrations.funpay.runner import RunnerCallbacks
from app.integrations.funpay.types import MessageInfo
from app.models.lot import Lot
from app.models.rental import Order, Rental
from app.services.order_provenance import (
    exact_lot_binding_exists,
    verified_sale_for_order_exists,
)
from app.services.command_handlers import (
    CodeHandler,
    HelpHandler,
    SubscriptionHandler,
    SellerHandler,
    ReplaceHandler,
)
from app.services.command_parser import CommandType
from app.services.command_router import CommandRouter, UnhandledMessage
from app.services.chat_service import ChatService, UnverifiedConversationError
from app.services.order_processor import OrderProcessor, LotNotFoundError
from app.services.sale_registry import SaleRegistryService
from app.services.rental_service import RentalService
from app.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


SessionFactory = Callable[[], AsyncSession]


def build_callbacks(
    session_factory: SessionFactory,
    gateway: ChatGateway,
    command_router: CommandRouter | None = None,
    sale_registry: SaleRegistryService | None = None,
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
    sale_registry = sale_registry or SaleRegistryService()
    router = command_router or CommandRouter()

    # Регистрируем хэндлеры команд для on_message.
    router.register(CommandType.CODE, CodeHandler())
    router.register(CommandType.HELP, HelpHandler())
    router.register(CommandType.SUBSCRIPTION, SubscriptionHandler())
    router.register(CommandType.SELLER, SellerHandler())
    router.register(CommandType.REPLACE, ReplaceHandler())

    async def on_new_sale(order_id: str) -> None:
        async with session_factory() as session:
            logger.info("Checking FunPay NewSale against bot-managed lots: %s", order_id)
            try:
                info = await gateway.get_order(order_id)
                order = await order_processor.process_new_sale(
                    session, gateway, order_id, info=info,
                )
                await sale_registry.register_new_sale(
                    session,
                    gateway,
                    order_id,
                    order=order,
                    info=info,
                )
                # Order создаётся и коммитится отдельно от fulfill_order,
                # чтобы сбой выдачи аккаунта не откатил сам заказ
                # (Order нужен для последующих on_sale_closed/refunded).
                await session.commit()
            except LotNotFoundError:
                await session.rollback()
                logger.info(
                    "Ignored FunPay sale %s: it is not a published bot lot",
                    order_id,
                )
                return
            except Exception:
                await session.rollback()
                logger.exception("Failed to process new sale %s", order_id)
                return

            notifier = await TelegramNotifier.from_settings(session)
            if notifier is not None:
                lot = await session.get(Lot, order.lot_id)
                description = lot.title_ru if lot is not None else "ChatGPT"
                await notifier.notify_new_order(order_id, description, order.price)

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
                sale = await sale_registry.update_status(
                    session, order_id, "completed"
                )
                if sale is None:
                    logger.info(
                        "Ignored close for unmanaged FunPay sale %s",
                        order_id,
                    )
                    await session.rollback()
                    return
                await order_processor.process_sale_closed(session, order_id)
                await session.commit()
                notifier = await TelegramNotifier.from_settings(session)
                if notifier is not None:
                    await notifier.notify_order_confirmed(order_id)
            except KeyError:
                await session.rollback()
                logger.info(
                    "Closed sale %s has no local fulfillment order", order_id
                )
            except Exception:
                await session.rollback()
                logger.exception("Failed to process sale closed %s", order_id)

    async def on_sale_refunded(order_id: str) -> None:
        async with session_factory() as session:
            try:
                sale = await sale_registry.update_status(
                    session, order_id, "refunded"
                )
                if sale is None:
                    logger.info(
                        "Ignored refund for unmanaged FunPay sale %s",
                        order_id,
                    )
                    await session.rollback()
                    return
                order = await order_processor.process_sale_refunded(session, order_id)
                await session.commit()
                notifier = await TelegramNotifier.from_settings(session)
                if notifier is not None:
                    await notifier.notify_order_refunded(
                        order_id,
                        pending=order.status == "refund_pending",
                    )
            except KeyError:
                await session.rollback()
                logger.info(
                    "Refunded sale %s has no local fulfillment order", order_id
                )
            except Exception:
                await session.rollback()
                logger.exception("Failed to process sale refunded %s", order_id)

    async def on_message(msg: MessageInfo) -> None:
        async with session_factory() as session:
            try:
                _, created = await chat_service.record_event(session, msg)
                await session.commit()
            except UnverifiedConversationError:
                await session.rollback()
                logger.info(
                    "Ignored unverified FunPay chat event chat=%s sender=%s",
                    msg.chat_id,
                    msg.sender_id,
                )
                return
            except IntegrityError:
                # A concurrent copy of the same FunPay event won the unique
                # source-id insert. Never execute a command without owning the
                # durable idempotency record.
                await session.rollback()
                logger.info("Duplicate message event in chat %s", msg.chat_id)
                return
            except Exception:
                await session.rollback()
                logger.exception("Failed to persist message in chat %s", msg.chat_id)
                return

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
            if ctx.parsed is not None and msg.order_id:
                # The selected command alias is an explicit language signal.
                # Persist it for later automated messages in the same order.
                order = (
                    await session.execute(
                        select(Order).where(
                            Order.funpay_order_id == msg.order_id,
                            Order.buyer_funpay_id == str(msg.sender_id),
                            Order.funpay_chat_id == str(msg.chat_id),
                            exact_lot_binding_exists(Order),
                            verified_sale_for_order_exists(Order),
                        )
                    )
                ).scalar_one_or_none()
                if order is not None:
                    order.buyer_locale = ctx.lang
                    rental = (
                        await session.execute(
                            select(Rental).where(Rental.order_id == order.id)
                        )
                    ).scalar_one_or_none()
                    if rental is not None:
                        rental.lang = ctx.lang
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
