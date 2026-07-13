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
            text="✅ FunPay Bot: Telegram notifications are configured.",
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

    async def notify_low_limits(
        self,
        account_login: str,
        *,
        remaining_pct: int,
        window_label: str,
    ) -> bool:
        return await self.notify(
            f"📊 Длинный лимит Codex аккаунта {account_login} "
            f"({window_label}) снизился до {remaining_pct}%"
        )

    async def notify_seller_called(
        self,
        buyer_id: str,
        *,
        funpay_chat_id: str | None = None,
        order_id: str | None = None,
    ) -> None:
        context = []
        if funpay_chat_id:
            context.append(f"чат FunPay #{funpay_chat_id}")
        if order_id:
            context.append(f"заказ #{order_id}")
        suffix = f" ({', '.join(context)})" if context else ""
        await self.notify(
            f"📢 Покупатель {buyer_id} вызывает продавца{suffix}. "
            "Ответьте ему в разделе «Чаты» админ-панели."
        )

    async def notify_order_refunded(self, order_id: str, *, pending: bool) -> None:
        state = "ожидает подтверждённого выхода из аккаунта" if pending else "возвращён"
        await self.notify(f"↩️ Заказ #{order_id}: {state}")

    async def notify_bump_failed(self, lot_id: int) -> None:
        await self.notify(f"❌ Bump лота #{lot_id} не удался")

    async def notify_funpay_disconnect(self) -> None:
        await self.notify("🔴 FunPay дисконнект / golden_key протух")
