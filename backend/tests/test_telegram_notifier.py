from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.telegram_notifier import TelegramNotifier


@pytest.fixture
def notifier() -> TelegramNotifier:
    return TelegramNotifier(bot_token="123:abc", seller_chat_id="456")


async def test_notify_sends_message(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify("Test message")
        mock_bot.send_message.assert_awaited_once_with(chat_id="456", text="Test message")


async def test_notify_swallows_error(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock(side_effect=RuntimeError("network"))
        await notifier.notify("Test")  # не должно падать


async def test_notify_new_order(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_new_order(order_id="123", desc="Plus x 7d", price=599)
        _, kwargs = mock_bot.send_message.call_args
        assert "123" in kwargs["text"]
        assert "599" in kwargs["text"]


async def test_notify_rental_expired(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_rental_expired(account_login="acc1")
        _, kwargs = mock_bot.send_message.call_args
        assert "acc1" in kwargs["text"]


async def test_notify_account_unavailable(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_account_unavailable(account_login="acc1", reason="ban")
        _, kwargs = mock_bot.send_message.call_args
        assert "acc1" in kwargs["text"]
        assert "ban" in kwargs["text"]


async def test_notify_low_limits(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        sent = await notifier.notify_low_limits(
            account_login="acc1",
            remaining_pct=18,
            window_label="30 дней",
        )
        _, kwargs = mock_bot.send_message.call_args
        assert sent is True
        assert "18" in kwargs["text"]
        assert "30 дней" in kwargs["text"]


async def test_notify_seller_called_includes_admin_chat_context(
    notifier: TelegramNotifier,
):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_seller_called(
            "buyer-1", funpay_chat_id="42", order_id="ABC"
        )
        _, kwargs = mock_bot.send_message.call_args
        assert "buyer-1" in kwargs["text"]
        assert "42" in kwargs["text"]
        assert "ABC" in kwargs["text"]
        assert "Чаты" in kwargs["text"]


async def test_disabled_when_no_token():
    n = TelegramNotifier(bot_token="", seller_chat_id="")
    await n.notify("test")  # silent no-op, не падает


async def test_from_settings_creates_notifier(session: AsyncSession):
    from app.models.settings import SellerSettings
    session.add(SellerSettings(
        id=1, telegram_bot_token="tok", telegram_seller_chat_id="chat",
    ))
    await session.flush()
    n = await TelegramNotifier.from_settings(session)
    assert n is not None
    assert n._seller_chat_id == "chat"
