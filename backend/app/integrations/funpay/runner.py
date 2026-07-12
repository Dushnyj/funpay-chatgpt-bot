from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.integrations.funpay.gateway import FunPayChatGateway
from app.integrations.funpay.types import MessageInfo


logger = logging.getLogger(__name__)


# Callback-типы: Фаза 4 подключит реальные реализации (OrderProcessor, CommandRouter)
NewSaleCallback = Callable[[str], Awaitable[None]]
SaleStatusCallback = Callable[[str], Awaitable[None]]
MessageCallback = Callable[[MessageInfo], Awaitable[None]]


@dataclass(frozen=True)
class SaleHandlers:
    """Хэндлеры событий заказа."""

    on_new_sale: NewSaleCallback | None = None
    on_sale_closed: SaleStatusCallback | None = None
    on_sale_refunded: SaleStatusCallback | None = None


@dataclass(frozen=True)
class MessageHandlers:
    """Хэндлер сообщений чата."""

    on_message: MessageCallback | None = None


@dataclass(frozen=True)
class RunnerCallbacks(SaleHandlers, MessageHandlers):
    """Все callback'и FunPay-событий в одном объекте.

    Атрибуты None — событие будет проигнорировано.
    """


class FunPayRunner:
    """Lifecycle-менеджер FunPay-соединения.

    Создаёт Bot + Dispatcher, регистрирует хэндлеры, управляет start/stop.
    Callback'и注入аются через RunnerCallbacks — Фаза 4 заполнит их реальными сервисами.
    """

    def __init__(
        self,
        golden_key: str,
        callbacks: RunnerCallbacks,
        category_id: int,
        *,
        bot=None,
        dispatcher=None,
        reconnect_delay: float = 5.0,
    ) -> None:
        self._golden_key = golden_key
        self.callbacks = callbacks
        self.category_id = category_id
        self._bot = bot
        self._dp = dispatcher
        self._gateway: FunPayChatGateway | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._reconnect_delay = reconnect_delay
        self._stopping = False
        self._handlers_registered = False
        self._started = False
        self.last_error: str | None = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def listener_task(self) -> asyncio.Task[None] | None:
        return self._listener_task

    @property
    def gateway(self) -> FunPayChatGateway:
        """Gateway bound to the very same Bot used by the event listener."""
        self._ensure_components()
        if self._gateway is None:
            self._gateway = FunPayChatGateway(self._bot)
        return self._gateway

    def set_callbacks(self, callbacks: RunnerCallbacks) -> None:
        if self._handlers_registered or self._started:
            raise RuntimeError("callbacks must be configured before runner start")
        self.callbacks = callbacks

    async def start(self) -> None:
        """Initialize Bot and start a tracked, reconnecting listener task."""
        if self._started:
            return
        self._ensure_components()
        self._register_handlers()
        await self._bot.update()
        self._stopping = False
        self.last_error = None
        self._listener_task = asyncio.create_task(
            self._listen_forever(), name="funpay-listener",
        )
        self._started = True

    async def stop(self) -> None:
        """Stop WebSocket loop and await the tracked listener task."""
        self._stopping = True
        if self._bot is not None and self._started:
            with suppress(Exception):
                await self._bot.stop_listening()
        if self._listener_task is not None:
            self._listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        self._started = False

    def _ensure_components(self) -> None:
        from funpaybotengine import Bot, Dispatcher

        if self._bot is None:
            self._bot = Bot(golden_key=self._golden_key)
        if self._dp is None:
            self._dp = Dispatcher()

    async def _listen_forever(self) -> None:
        while not self._stopping:
            try:
                await self._bot.listen_events(self._dp)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("FunPay listener failed; reconnecting")
            else:
                if not self._stopping:
                    self.last_error = "FunPay listener stopped unexpectedly"
                    logger.warning(self.last_error)

            if not self._stopping:
                await asyncio.sleep(self._reconnect_delay)

    def _register_handlers(self) -> None:
        """Регистрация хэндлеров событий в Dispatcher."""
        if self._dp is None:
            return
        if self._handlers_registered:
            return

        if self.callbacks.on_new_sale is not None:
            new_sale_cb = self.callbacks.on_new_sale

            @self._dp.on_new_sale()
            async def handle_new_sale(event):
                order_id = event.object.meta.order_id
                await new_sale_cb(order_id)

        if self.callbacks.on_sale_closed is not None:
            sale_closed_cb = self.callbacks.on_sale_closed

            @self._dp.on_sale_closed()
            async def handle_sale_closed(event):
                order_id = event.object.meta.order_id
                await sale_closed_cb(order_id)

        if self.callbacks.on_sale_refunded is not None:
            sale_refunded_cb = self.callbacks.on_sale_refunded

            @self._dp.on_sale_refunded()
            async def handle_sale_refunded(event):
                order_id = event.object.meta.order_id
                await sale_refunded_cb(order_id)

        if self.callbacks.on_message is not None:
            message_cb = self.callbacks.on_message

            @self._dp.on_new_message()
            async def handle_message(event):
                msg = event.message
                info = MessageInfo(
                    message_id=msg.id,
                    chat_id=int(msg.chat_id) if msg.chat_id else 0,
                    sender_id=msg.sender_id,
                    text=msg.text,
                    order_id=msg.meta.order_id if msg.meta else None,
                    from_me=bool(msg.from_me),
                )
                await message_cb(info)

        self._handlers_registered = True
