from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from app.integrations.funpay.gateway import ChatGateway
from app.services.command_parser import CommandParser, ParsedCommand


@dataclass(frozen=True)
class CommandContext:
    """Контекст обработки сообщения из чата FunPay.

    Передаётся в хэндлер команды. Содержит всё для ответа и бизнес-логики.
    """

    chat_id: int
    sender_id: int
    text: str
    order_id: str | None
    lang: str
    gateway: ChatGateway
    parsed: ParsedCommand | None


CommandHandler = Callable[[CommandContext], Awaitable[None]]


class UnhandledMessage(Exception):
    """Команда распознана, но для неё нет зарегистрированного хэндлера."""


class CommandRouter:
    """Диспетчер команд: парсит → маршрутизирует в зарегистрированный хэндлер.

    Хэндлеры регистрируются по CommandType (Фаза 4 подключит реальные сервисы).
    Нераспознанные сообщения игнорируются (return None).
    Распознанная команда без хэндлера → UnhandledMessage.
    """

    def __init__(self, parser: CommandParser | None = None) -> None:
        self._parser = parser or CommandParser()
        self._handlers: dict = {}

    def register(self, command, handler: CommandHandler) -> None:
        self._handlers[command] = handler

    def build_context(
        self,
        chat_id: int,
        sender_id: int,
        text: str,
        order_id: str | None,
        lang: str,
        gateway: ChatGateway,
    ) -> CommandContext:
        parsed = self._parser.parse(text)
        return CommandContext(
            chat_id=chat_id,
            sender_id=sender_id,
            text=text,
            order_id=order_id,
            lang=parsed.lang if parsed is not None else lang,
            gateway=gateway,
            parsed=parsed,
        )

    async def dispatch(self, ctx: CommandContext) -> None:
        if ctx.parsed is None:
            return
        handler = self._handlers.get(ctx.parsed.command)
        if handler is None:
            raise UnhandledMessage(
                f"No handler registered for {ctx.parsed.command}"
            )
        await handler(ctx)
