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
        assert kwargs["text"] == (
            "Новый заказ\n"
            "Заказ: #123\n"
            "Позиция: Plus x 7d\n"
            "Сумма: 599 ₽"
        )


async def test_notify_rental_expired(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_rental_expired(account_login="acc1")
        _, kwargs = mock_bot.send_message.call_args
        assert kwargs["text"] == "Аренда завершена\nАккаунт: acc1"


async def test_notify_account_unavailable(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_account_unavailable(account_login="acc1", reason="ban")
        _, kwargs = mock_bot.send_message.call_args
        assert kwargs["text"] == (
            "Аккаунт недоступен\nАккаунт: acc1\nПричина: ban"
        )


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
        assert kwargs["text"] == (
            "Низкий лимит Codex\n"
            "Аккаунт: acc1\n"
            "Окно: 30 дней\n"
            "Остаток: 18%"
        )


async def test_notify_seller_called_includes_admin_chat_context(
    notifier: TelegramNotifier,
):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_seller_called(
            "buyer-1", funpay_chat_id="42", order_id="ABC"
        )
        _, kwargs = mock_bot.send_message.call_args
        assert kwargs["text"] == (
            "Покупатель вызвал продавца\n"
            "Покупатель: buyer-1\n"
            "Чат FunPay: #42\n"
            "Заказ: #ABC\n\n"
            "Ответьте покупателю в разделе «Чаты» админ-панели."
        )


async def test_send_test_uses_operator_facing_russian_copy(
    notifier: TelegramNotifier,
):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.send_test()

        mock_bot.send_message.assert_awaited_once_with(
            chat_id="456",
            text=(
                "Связь с Telegram настроена\n"
                "FunPay Bot готов отправлять уведомления."
            ),
        )


@pytest.mark.parametrize(
    ("method", "args", "kwargs", "expected"),
    [
        (
            "notify_order_confirmed",
            ("A1",),
            {},
            "Заказ подтверждён\nЗаказ: #A1",
        ),
        (
            "notify_dispute",
            ("A2",),
            {},
            "Требуется внимание\nОткрыт спор по заказу #A2.",
        ),
        (
            "notify_replace_requested",
            ("buyer", "login@example.test"),
            {},
            (
                "Запрос на замену\n"
                "Покупатель: buyer\n"
                "Аккаунт: login@example.test"
            ),
        ),
        (
            "notify_order_refunded",
            ("A3",),
            {"pending": True},
            (
                "Возврат заказа\n"
                "Заказ: #A3\n"
                "Статус: выход из аккаунта ещё не подтверждён"
            ),
        ),
        (
            "notify_order_refunded",
            ("A4",),
            {"pending": False},
            "Возврат заказа\nЗаказ: #A4\nСтатус: возврат завершён",
        ),
        (
            "notify_bump_failed",
            (17,),
            {},
            "Не удалось поднять лот\nЛот: #17",
        ),
        (
            "notify_funpay_disconnect",
            (),
            {},
            (
                "Потеряно соединение с FunPay\n"
                "Проверьте подключение и golden_key в настройках."
            ),
        ),
    ],
)
async def test_admin_notifications_use_structured_copy(
    notifier: TelegramNotifier,
    method: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    expected: str,
):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await getattr(notifier, method)(*args, **kwargs)

        assert mock_bot.send_message.await_args.kwargs["text"] == expected


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
