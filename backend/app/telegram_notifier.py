from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Bot

from app.models.settings import SellerSettings
from app.config import get_settings

logger = logging.getLogger(__name__)


async def get_effective_telegram_config(
    session: AsyncSession,
) -> tuple[str, str]:
    settings = await session.get(SellerSettings, 1)
    app_settings = get_settings()
    token = (
        settings.telegram_bot_token
        if settings is not None and settings.telegram_bot_token
        else app_settings.telegram_bot_token
    )
    chat_id = (
        settings.telegram_seller_chat_id
        if settings is not None and settings.telegram_seller_chat_id
        else app_settings.telegram_seller_chat_id
    )
    return token or "", chat_id or ""


class TelegramNotifier:
    """Микро-бот: только отправка сообщений продавцу в seller_chat_id.

    Все методы swallow exceptions — уведомления не должны ломать основной поток.
    Если bot_token пустой — все методы no-op.
    """

    def __init__(self, bot_token: str, seller_chat_id: str) -> None:
        self._seller_chat_id = seller_chat_id
        self._bot = Bot(token=bot_token) if bot_token else None

    @classmethod
    async def from_settings(cls, session: AsyncSession) -> TelegramNotifier | None:
        token, chat_id = await get_effective_telegram_config(session)
        if not token or not chat_id:
            return None
        try:
            return cls(bot_token=token, seller_chat_id=chat_id)
        except Exception:
            logger.exception("Invalid Telegram notification configuration")
            return None

    async def send_test(self) -> None:
        """Send a test notification and surface errors to the settings API."""
        if self._bot is None or not self._seller_chat_id:
            raise RuntimeError("Telegram is not configured")
        await self._bot.send_message(
            chat_id=self._seller_chat_id,
            text=(
                "Связь с Telegram настроена\n"
                "FunPay Bot готов отправлять уведомления."
            ),
        )

    async def notify(self, text: str) -> bool:
        if self._bot is None or not self._seller_chat_id:
            return False
        try:
            await self._bot.send_message(chat_id=self._seller_chat_id, text=text)
            return True
        except Exception:
            logger.exception("Telegram notify failed")
            return False

    async def notify_new_order(self, order_id: str, desc: str, price: int) -> None:
        await self.notify(
            "Новый заказ\n"
            f"Заказ: #{order_id}\n"
            f"Позиция: {desc}\n"
            f"Сумма: {price} ₽"
        )

    async def notify_order_confirmed(self, order_id: str) -> None:
        await self.notify(f"Заказ подтверждён\nЗаказ: #{order_id}")

    async def notify_dispute(self, order_id: str) -> None:
        await self.notify(
            f"Требуется внимание\nОткрыт спор по заказу #{order_id}."
        )

    async def notify_replace_requested(self, buyer_id: str, account_login: str) -> None:
        await self.notify(
            "Запрос на замену\n"
            f"Покупатель: {buyer_id}\n"
            f"Аккаунт: {account_login}"
        )

    async def notify_rental_expired(self, account_login: str) -> None:
        await self.notify(
            "Аренда завершена\n"
            f"Аккаунт: {account_login}"
        )

    async def notify_account_unavailable(self, account_login: str, reason: str) -> None:
        await self.notify(
            "Аккаунт недоступен\n"
            f"Аккаунт: {account_login}\n"
            f"Причина: {reason}"
        )

    async def notify_low_limits(
        self,
        account_login: str,
        *,
        remaining_pct: int,
        window_label: str,
    ) -> bool:
        return await self.notify(
            "Низкий лимит Codex\n"
            f"Аккаунт: {account_login}\n"
            f"Окно: {window_label}\n"
            f"Остаток: {remaining_pct}%"
        )

    async def notify_seller_called(
        self,
        buyer_id: str,
        *,
        funpay_chat_id: str | None = None,
        order_id: str | None = None,
    ) -> None:
        context = [
            "Покупатель вызвал продавца",
            f"Покупатель: {buyer_id}",
        ]
        if funpay_chat_id:
            context.append(f"Чат FunPay: #{funpay_chat_id}")
        if order_id:
            context.append(f"Заказ: #{order_id}")
        context.extend(
            (
                "",
                "Ответьте покупателю в разделе «Чаты» админ-панели.",
            )
        )
        await self.notify("\n".join(context))

    async def notify_order_refunded(self, order_id: str, *, pending: bool) -> None:
        state = (
            "выход из аккаунта ещё не подтверждён"
            if pending
            else "возврат завершён"
        )
        await self.notify(
            "Возврат заказа\n"
            f"Заказ: #{order_id}\n"
            f"Статус: {state}"
        )

    async def notify_bump_failed(self, lot_id: int) -> None:
        await self.notify(f"Не удалось поднять лот\nЛот: #{lot_id}")

    async def notify_funpay_disconnect(self) -> None:
        await self.notify(
            "Потеряно соединение с FunPay\n"
            "Проверьте подключение и golden_key в настройках."
        )
