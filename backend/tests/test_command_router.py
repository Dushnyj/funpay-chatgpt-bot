from unittest.mock import AsyncMock

import pytest

from app.integrations.funpay.gateway import FakeChatGateway
from app.services.command_parser import CommandType
from app.services.command_router import (
    CommandRouter,
    CommandContext,
    UnhandledMessage,
)


@pytest.fixture
def router() -> CommandRouter:
    return CommandRouter()


@pytest.fixture
def gateway() -> FakeChatGateway:
    return FakeChatGateway()


def _ctx(router: CommandRouter, gateway: FakeChatGateway, text: str, chat_id: int = 100,
         order_id: str | None = "500", lang: str = "ru") -> CommandContext:
    return router.build_context(
        chat_id=chat_id,
        sender_id=200,
        text=text,
        order_id=order_id,
        lang=lang,
        gateway=gateway,
    )


async def test_route_code_calls_registered_handler(router: CommandRouter, gateway: FakeChatGateway):
    handler = AsyncMock()
    router.register(CommandType.CODE, handler)
    ctx = _ctx(router, gateway, "!код")
    await router.dispatch(ctx)
    handler.assert_awaited_once_with(ctx)


async def test_route_unknown_command_does_nothing(router: CommandRouter, gateway: FakeChatGateway):
    handler = AsyncMock()
    router.register(CommandType.CODE, handler)
    ctx = _ctx(router, gateway, "привет")
    await router.dispatch(ctx)
    handler.assert_not_awaited()


async def test_unregistered_command_raises_unhandled(router: CommandRouter, gateway: FakeChatGateway):
    ctx = _ctx(router, gateway, "!помощь")
    with pytest.raises(UnhandledMessage):
        await router.dispatch(ctx)


async def test_register_overwrites_previous(router: CommandRouter, gateway: FakeChatGateway):
    first = AsyncMock()
    second = AsyncMock()
    router.register(CommandType.CODE, first)
    router.register(CommandType.CODE, second)
    ctx = _ctx(router, gateway, "!код")
    await router.dispatch(ctx)
    first.assert_not_awaited()
    second.assert_awaited_once_with(ctx)


def test_build_context_parses_command(router: CommandRouter, gateway: FakeChatGateway):
    ctx = _ctx(router, gateway, "!код")
    assert ctx.parsed is not None
    assert ctx.parsed.command is CommandType.CODE


def test_build_context_none_parsed_for_non_command(router: CommandRouter, gateway: FakeChatGateway):
    ctx = _ctx(router, gateway, "обычное сообщение")
    assert ctx.parsed is None
