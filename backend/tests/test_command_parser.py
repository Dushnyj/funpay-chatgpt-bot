import pytest

from app.services.command_parser import (
    CommandParser,
    CommandType,
    ParsedCommand,
)


@pytest.fixture
def parser() -> CommandParser:
    return CommandParser()


def test_parse_code_ru(parser: CommandParser):
    result = parser.parse("!код")
    assert result is not None
    assert result.command is CommandType.CODE
    assert result.argument is None
    assert result.lang == "ru"


def test_parse_code_en(parser: CommandParser):
    result = parser.parse("!code")
    assert result is not None
    assert result.command is CommandType.CODE
    assert result.lang == "en"


def test_parse_case_insensitive(parser: CommandParser):
    assert parser.parse("!КОД").command is CommandType.CODE
    assert parser.parse("!Code").command is CommandType.CODE
    assert parser.parse("!SUB").command is CommandType.SUBSCRIPTION


def test_parse_subscription_ru(parser: CommandParser):
    assert parser.parse("!подписка").command is CommandType.SUBSCRIPTION


def test_parse_subscription_en(parser: CommandParser):
    assert parser.parse("!sub").command is CommandType.SUBSCRIPTION


def test_parse_replace_ru(parser: CommandParser):
    assert parser.parse("!замена").command is CommandType.REPLACE


def test_parse_replace_en(parser: CommandParser):
    assert parser.parse("!replace").command is CommandType.REPLACE


def test_parse_seller_ru(parser: CommandParser):
    assert parser.parse("!продавец").command is CommandType.SELLER


def test_parse_seller_en(parser: CommandParser):
    assert parser.parse("!seller").command is CommandType.SELLER


def test_parse_help_ru(parser: CommandParser):
    assert parser.parse("!помощь").command is CommandType.HELP


def test_parse_help_en(parser: CommandParser):
    assert parser.parse("!help").command is CommandType.HELP


def test_parse_with_argument(parser: CommandParser):
    result = parser.parse("!код что-то лишнее")
    assert result is not None
    assert result.command is CommandType.CODE
    assert result.argument == "что-то лишнее"
    assert result.order_reference is None
    assert result.order_reference_invalid is True


@pytest.mark.parametrize(
    ("text", "command", "lang"),
    [
        ("!код #hhhgnz4n", CommandType.CODE, "ru"),
        ("!code #HHHGNZ4N", CommandType.CODE, "en"),
        ("!подписка #hhhgnz4n", CommandType.SUBSCRIPTION, "ru"),
        ("!sub #HHHGNZ4N", CommandType.SUBSCRIPTION, "en"),
        ("!замена #hhhgnz4n", CommandType.REPLACE, "ru"),
        ("!replace #HHHGNZ4N", CommandType.REPLACE, "en"),
    ],
)
def test_parse_order_qualified_buyer_commands(
    parser: CommandParser,
    text: str,
    command: CommandType,
    lang: str,
):
    result = parser.parse(text)

    assert result == ParsedCommand(
        command=command,
        argument=text.split(maxsplit=1)[1],
        lang=lang,
        order_reference="HHHGNZ4N",
        order_reference_invalid=False,
    )


@pytest.mark.parametrize(
    "argument",
    [
        "HHHGNZ4N",
        "#SHORT",
        "#TOOLONG99",
        "#HHHGNZ4N extra",
        "##HHHGNZ4N",
        "#HHHGNZ4_",
        "#НННGNZ4N",
        "https://funpay.com/orders/HHHGNZ4N/",
    ],
)
def test_parse_rejects_non_exact_order_reference(
    parser: CommandParser,
    argument: str,
):
    result = parser.parse(f"!code {argument}")

    assert result is not None
    assert result.order_reference is None
    assert result.order_reference_invalid is True


def test_parse_non_command_returns_none(parser: CommandParser):
    assert parser.parse("привет") is None
    assert parser.parse("hello world") is None
    assert parser.parse("") is None


def test_parse_exclamation_no_match(parser: CommandParser):
    assert parser.parse("!неизвестная") is None


def test_parse_ignores_leading_whitespace(parser: CommandParser):
    result = parser.parse("  !код")
    assert result is not None
    assert result.command is CommandType.CODE


def test_parsed_command_is_frozen(parser: CommandParser):
    result = parser.parse("!код")
    with pytest.raises(Exception):
        result.command = CommandType.HELP  # type: ignore
