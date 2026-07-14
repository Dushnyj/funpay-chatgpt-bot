from __future__ import annotations

import re
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
    order_reference: str | None = None
    order_reference_invalid: bool = False


_ORDER_REFERENCE_PATTERN = re.compile(r"#([A-Za-z0-9]{8})\Z")
_ORDER_QUALIFIED_COMMANDS = frozenset(
    {CommandType.CODE, CommandType.SUBSCRIPTION, CommandType.REPLACE}
)


def normalize_order_reference(argument: str | None) -> str | None:
    """Return one canonical FunPay order id from a strict command argument.

    Buyer commands accept only the copyable FunPay form ``#HHHGNZ4N``:
    exactly eight ASCII letters/digits, with no URL, prose, or second token.
    The database-facing value never contains ``#`` and is upper-cased.
    """

    if argument is None:
        return None
    matched = _ORDER_REFERENCE_PATTERN.fullmatch(argument)
    if matched is None:
        return None
    return matched.group(1).upper()


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
        order_reference = (
            normalize_order_reference(argument)
            if cmd in _ORDER_QUALIFIED_COMMANDS
            else None
        )
        return ParsedCommand(
            command=cmd,
            argument=argument,
            lang=lang,
            order_reference=order_reference,
            order_reference_invalid=(
                cmd in _ORDER_QUALIFIED_COMMANDS
                and argument is not None
                and order_reference is None
            ),
        )
