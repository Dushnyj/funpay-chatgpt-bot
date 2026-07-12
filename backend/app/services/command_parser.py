from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class CommandType(Enum):
    """Типы команд, поддерживаемых ботом в чатах сделок FunPay."""

    CODE = auto()
    SUBSCRIPTION = auto()
    REPLACE = auto()
    SELLER = auto()
    HELP = auto()


@dataclass(frozen=True)
class ParsedCommand:
    """Результат парсинга: определённая команда + хвост текста как аргумент."""

    command: CommandType
    argument: str | None


# Алиасы RU/EN для каждой команды. Match по lowercased префиксу без `!`.
_ALIASES: dict[str, CommandType] = {
    "код": CommandType.CODE,
    "code": CommandType.CODE,
    "подписка": CommandType.SUBSCRIPTION,
    "sub": CommandType.SUBSCRIPTION,
    "замена": CommandType.REPLACE,
    "replace": CommandType.REPLACE,
    "продавец": CommandType.SELLER,
    "seller": CommandType.SELLER,
    "помощь": CommandType.HELP,
    "help": CommandType.HELP,
}


class CommandParser:
    """Парсер команд из текста сообщений чата FunPay.

    Команда — префикс `!` + алиас (case-insensitive). Остаток строки — аргумент.
    """

    def parse(self, text: str | None) -> ParsedCommand | None:
        if not text:
            return None
        stripped = text.strip()
        if not stripped.startswith("!"):
            return None
        body = stripped[1:].strip()
        if not body:
            return None
        parts = body.split(maxsplit=1)
        alias = parts[0].lower()
        cmd = _ALIASES.get(alias)
        if cmd is None:
            return None
        argument = parts[1].strip() if len(parts) > 1 else None
        return ParsedCommand(command=cmd, argument=argument)
