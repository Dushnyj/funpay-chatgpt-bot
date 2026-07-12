from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Bot

from app.models.settings import SellerSettings

logger = logging.getLogger(__name__)


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
        settings = await session.get(SellerSettings, 1)
        if settings is None or not settings.telegram_bot_token:
            return None
        return cls(
            bot_token=settings.telegram_bot_token or "",
            seller_chat_id=settings.telegram_seller_chat_id or "",
        )

    async def notify(self, text: str) -> None:
        if self._bot is None or not self._seller_chat_id:
            return
        try:
            await self._bot.send_message(chat_id=self._seller_chat_id, text=text)
        except Exception:
            logger.exception("Telegram notify failed")

    async def notify_new_order(self, order_id: str, desc: str, price: int) -> None:
        await self.notify(f"🆕 Новый заказ #{order_id}: {desc}, {price}₽")

    async def notify_order_confirmed(self, order_id: str) -> None:
        await self.notify(f"✅ Заказ #{order_id} подтверждён")

    async def notify_dispute(self, order_id: str) -> None:
        await self.notify(f"⚠️ СПОР по заказу #{order_id}!")

    async def notify_replace_requested(self, buyer_id: str, account_login: str) -> None:
        await self.notify(f"🔄 Замена: покупатель {buyer_id} запросил замену аккаунта {account_login}")

    async def notify_rental_expired(self, account_login: str) -> None:
        await self.notify(f"⏰ Аренда истекла: аккаунт {account_login}, освободился слот")

    async def notify_account_unavailable(self, account_login: str, reason: str) -> None:
        await self.notify(f"🔴 Аккаунт {account_login} недоступен ({reason})")

    async def notify_low_limits(self, account_login: str, chat_weekly: int) -> None:
        await self.notify(f"📊 Лимиты аккаунта {account_login} упали ниже порога (chat weekly: {chat_weekly}%)")

    async def notify_seller_called(self, buyer_id: str) -> None:
        await self.notify(f"📢 Покупатель {buyer_id} вызывает продавца")

    async def notify_bump_failed(self, lot_id: int) -> None:
        await self.notify(f"❌ Bump лота #{lot_id} не удался")

    async def notify_funpay_disconnect(self) -> None:
        await self.notify("🔴 FunPay дисконнект / golden_key протух")
