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
    lang: str = "ru"


# Алиасы RU/EN для каждой команды. Match по lowercased префиксу без `!`.
_ALIASES: dict[str, tuple[CommandType, str]] = {
    "код": (CommandType.CODE, "ru"),
    "code": (CommandType.CODE, "en"),
    "подписка": (CommandType.SUBSCRIPTION, "ru"),
    "sub": (CommandType.SUBSCRIPTION, "en"),
    "замена": (CommandType.REPLACE, "ru"),
    "replace": (CommandType.REPLACE, "en"),
    "продавец": (CommandType.SELLER, "ru"),
    "seller": (CommandType.SELLER, "en"),
    "помощь": (CommandType.HELP, "ru"),
    "help": (CommandType.HELP, "en"),
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
        resolved = _ALIASES.get(alias)
        if resolved is None:
            return None
        cmd, lang = resolved
        argument = parts[1].strip() if len(parts) > 1 else None
        return ParsedCommand(command=cmd, argument=argument, lang=lang)
