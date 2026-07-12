from unittest.mock import AsyncMock

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
