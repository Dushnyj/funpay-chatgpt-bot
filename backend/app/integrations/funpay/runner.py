from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from app.integrations.funpay.types import MessageInfo


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
    ) -> None:
        self._golden_key = golden_key
        self.callbacks = callbacks
        self.category_id = category_id
        self._bot = None
        self._dp = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Инициализация Bot, регистрация хэндлеров, запуск WebSocket loop."""
        from funpaybotengine import Bot, Dispatcher

        self._bot = Bot(golden_key=self._golden_key)
        self._dp = Dispatcher()
        self._register_handlers()
        await self._bot.update()
        self._started = True
        import asyncio
        asyncio.create_task(self._bot.listen_events(self._dp))

    async def stop(self) -> None:
        """Остановка WebSocket loop."""
        if self._bot and self._started:
            await self._bot.stop_listening()
        self._started = False

    def _register_handlers(self) -> None:
        """Регистрация хэндлеров событий в Dispatcher."""
        if self._dp is None:
            return

        if self.callbacks.on_new_sale is not None:
            cb = self.callbacks.on_new_sale

            @self._dp.on_new_sale()
            async def handle_new_sale(event):
                order_id = event.object.meta.order_id
                await cb(order_id)

        if self.callbacks.on_sale_closed is not None:
            cb = self.callbacks.on_sale_closed

            @self._dp.on_sale_closed()
            async def handle_sale_closed(event):
                order_id = event.object.meta.order_id
                await cb(order_id)

        if self.callbacks.on_sale_refunded is not None:
            cb = self.callbacks.on_sale_refunded

            @self._dp.on_sale_refunded()
            async def handle_sale_refunded(event):
                order_id = event.object.meta.order_id
                await cb(order_id)

        if self.callbacks.on_message is not None:
            cb = self.callbacks.on_message

            @self._dp.on_new_message()
            async def handle_message(event):
                msg = event.message
                info = MessageInfo(
                    message_id=msg.id,
                    chat_id=int(msg.chat_id) if msg.chat_id else 0,
                    sender_id=msg.sender_id,
                    text=msg.text,
                    order_id=msg.meta.order_id if msg.meta else None,
                )
                await cb(info)
