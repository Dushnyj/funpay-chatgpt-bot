import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from app.integrations.funpay.runner import (
    FunPayRunner,
    SaleHandlers,
    MessageHandlers,
    RunnerCallbacks,
)


def test_runner_callbacks_defaults():
    callbacks = RunnerCallbacks()
    assert callbacks.on_new_sale is None
    assert callbacks.on_sale_closed is None
    assert callbacks.on_sale_refunded is None
    assert callbacks.on_message is None


def test_sale_handlers_stores_callbacks():
    new_sale = AsyncMock()
    closed = AsyncMock()
    refunded = AsyncMock()
    handlers = SaleHandlers(on_new_sale=new_sale, on_sale_closed=closed, on_sale_refunded=refunded)
    assert handlers.on_new_sale is new_sale


def test_message_handlers_stores_callback():
    on_msg = AsyncMock()
    handlers = MessageHandlers(on_message=on_msg)
    assert handlers.on_message is on_msg


def test_runner_callbacks_from_handlers():
    new_sale = AsyncMock()
    on_msg = AsyncMock()
    callbacks = RunnerCallbacks(
        on_new_sale=new_sale,
        on_message=on_msg,
    )
    assert callbacks.on_new_sale is new_sale
    assert callbacks.on_message is on_msg


def test_runner_stores_config():
    runner = FunPayRunner(
        golden_key="test-key",
        callbacks=RunnerCallbacks(),
        category_id=1,
    )
    assert runner.category_id == 1
    assert runner.callbacks.on_new_sale is None


def test_runner_callbacks_is_dataclass():
    from dataclasses import is_dataclass
    assert is_dataclass(RunnerCallbacks)
    assert is_dataclass(SaleHandlers)
    assert is_dataclass(MessageHandlers)


async def test_runner_maps_message_from_me_flag():
    class Dispatcher:
        message_handler = None

        def on_new_message(self):
            def decorator(handler):
                self.message_handler = handler
                return handler
            return decorator

    callback = AsyncMock()
    dispatcher = Dispatcher()
    runner = FunPayRunner(
        golden_key="test-key",
        callbacks=RunnerCallbacks(on_message=callback),
        category_id=1,
        bot=object(),
        dispatcher=dispatcher,
    )
    runner._register_handlers()
    event = SimpleNamespace(message=SimpleNamespace(
        id=10,
        chat_id=20,
        sender_id=30,
        text="seller reply",
        meta=None,
        from_me=True,
    ))

    await dispatcher.message_handler(event)

    mapped = callback.await_args.args[0]
    assert mapped.from_me is True


async def test_runner_tracks_listener_and_gateway_uses_same_bot():
    seen_config = None

    async def _listen(_dp, *, config):
        nonlocal seen_config
        seen_config = config
        await asyncio.Event().wait()

    bot = SimpleNamespace(
        update=AsyncMock(),
        listen_events=AsyncMock(side_effect=_listen),
        stop_listening=AsyncMock(),
        userid=123,
        username="seller",
        session=SimpleNamespace(close=AsyncMock()),
    )
    runner = FunPayRunner(
        "key", RunnerCallbacks(), 1,
        bot=bot, dispatcher=object(), reconnect_delay=0.01,
    )

    await runner.prepare()
    assert runner.started is False
    assert runner.listener_task is None
    bot.update.assert_awaited_once()

    await runner.start()

    assert runner.started is True
    assert runner.listener_task is not None
    assert runner.gateway._bot is bot
    await asyncio.sleep(0)
    assert seen_config is not None
    assert seen_config.discover_sales is True
    assert seen_config.discover_purchases is False
    bot.update.assert_awaited_once()
    await runner.stop()
    assert runner.listener_task is None
    bot.stop_listening.assert_awaited_once()
    bot.session.close.assert_awaited_once()


async def test_runner_prepare_rejects_anonymous_or_invalid_session():
    from app.integrations.funpay.exceptions import GoldenKeyError

    bot = SimpleNamespace(
        update=AsyncMock(),
        userid=-1,
        username="",
    )
    runner = FunPayRunner(
        "invalid-key", RunnerCallbacks(), 1,
        bot=bot, dispatcher=object(),
    )

    with pytest.raises(GoldenKeyError):
        await runner.prepare()

    assert runner.started is False
    assert runner._prepared is False


async def test_runner_sale_handlers_keep_distinct_callbacks():
    class Dispatcher:
        handlers = {}

        def _decorator(self, name):
            def register(handler):
                self.handlers[name] = handler
                return handler
            return register

        def on_new_sale(self):
            return self._decorator("new")

        def on_sale_closed(self):
            return self._decorator("closed")

        def on_sale_refunded(self):
            return self._decorator("refunded")

    new = AsyncMock()
    closed = AsyncMock()
    refunded = AsyncMock()
    dispatcher = Dispatcher()
    runner = FunPayRunner(
        "key",
        RunnerCallbacks(
            on_new_sale=new,
            on_sale_closed=closed,
            on_sale_refunded=refunded,
        ),
        1,
        bot=object(),
        dispatcher=dispatcher,
    )
    runner._register_handlers()
    event = SimpleNamespace(object=SimpleNamespace(meta=SimpleNamespace(order_id="O-1")))

    await dispatcher.handlers["new"](event)
    await dispatcher.handlers["closed"](event)
    await dispatcher.handlers["refunded"](event)

    new.assert_awaited_once_with("O-1")
    closed.assert_awaited_once_with("O-1")
    refunded.assert_awaited_once_with("O-1")
