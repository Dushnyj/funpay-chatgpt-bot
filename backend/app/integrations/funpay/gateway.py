from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.integrations.funpay.types import (
    OrderInfo,
    OfferInfo,
    OfferFieldsDTO,
)


@runtime_checkable
class ChatGateway(Protocol):
    """Абстракция над FunPay-соединением. Изолирует домен от funpaybotengine."""

    async def send_message(self, chat_id: int, text: str) -> int:
        """Отправить текстовое сообщение в чат. Возвращает message_id."""
        ...

    async def get_order(self, order_id: str) -> OrderInfo:
        """Получить данные заказа по ID."""
        ...

    async def save_offer_fields(self, fields: OfferFieldsDTO) -> int:
        """Создать (offer_id=0) или обновить существующий лот. Возвращает offer_id."""
        ...

    async def set_offer_active(self, offer_id: int, active: bool) -> bool:
        """Активировать/поставить на паузу отдельный лот."""
        ...

    async def get_my_offers(self, subcategory_id: int) -> list[OfferInfo]:
        """Получить список своих лотов в подкатегории."""
        ...

    async def bump_category(self, category_id: int, subcategory_id: int) -> bool:
        """Поднять все лоты подкатегории (FunPay bump)."""
        ...


class FakeChatGateway:
    """Тестовый double ChatGateway. Записывает все вызовы для assert'ов.

    НЕ используется в продакшене — только в тестах сервисов.
    """

    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []
        self._next_message_id = 1
        self._next_offer_id = 1
        self._orders: dict[str, OrderInfo] = {}
        self.saved_offers: dict[int, OfferFieldsDTO] = {}
        self.activity_changes: list[tuple[int, bool]] = []
        self.bumped: list[tuple[int, int]] = []
        self._my_offers: dict[int, list[OfferInfo]] = {}

    def set_order(self, order: OrderInfo) -> None:
        self._orders[order.order_id] = order

    def set_my_offers(self, subcategory_id: int, offers: list[OfferInfo]) -> None:
        self._my_offers[subcategory_id] = offers

    async def send_message(self, chat_id: int, text: str) -> int:
        msg_id = self._next_message_id
        self._next_message_id += 1
        self.sent_messages.append((chat_id, text))
        return msg_id

    async def get_order(self, order_id: str) -> OrderInfo:
        if order_id not in self._orders:
            raise KeyError(order_id)
        return self._orders[order_id]

    async def save_offer_fields(self, fields: OfferFieldsDTO) -> int:
        if fields.offer_id == 0:
            offer_id = self._next_offer_id
            self._next_offer_id += 1
            updated = OfferFieldsDTO(
                offer_id=offer_id,
                subcategory_id=fields.subcategory_id,
                title_ru=fields.title_ru,
                title_en=fields.title_en,
                desc_ru=fields.desc_ru,
                desc_en=fields.desc_en,
                price=fields.price,
                active=fields.active,
                auto_delivery=fields.auto_delivery,
            )
            self.saved_offers[offer_id] = updated
            return offer_id
        self.saved_offers[fields.offer_id] = fields
        return fields.offer_id

    async def set_offer_active(self, offer_id: int, active: bool) -> bool:
        self.activity_changes.append((offer_id, active))
        return True

    async def get_my_offers(self, subcategory_id: int) -> list[OfferInfo]:
        return self._my_offers.get(subcategory_id, [])

    async def bump_category(self, category_id: int, subcategory_id: int) -> bool:
        self.bumped.append((category_id, subcategory_id))
        return True


from funpayparsers.types.enums import OrderStatus as _FPOrderStatus

from app.integrations.funpay.types import SaleStatus


def _map_order_status(fp_status: _FPOrderStatus) -> SaleStatus:
    """Маппинг OrderStatus из funpayparsers → наш SaleStatus enum."""
    return {
        _FPOrderStatus.PAID: SaleStatus.PAID,
        _FPOrderStatus.COMPLETED: SaleStatus.COMPLETED,
        _FPOrderStatus.REFUNDED: SaleStatus.REFUNDED,
    }.get(fp_status, SaleStatus.UNKNOWN)


def _build_order_info(page) -> OrderInfo:
    """Сборка OrderInfo из OrderPage funpaybotengine.

    page — объект funpaybotengine.types.pages.order_page.OrderPage.
    Chat.interlocutor — UserPreview (покупатель для sale-заказа).
    MoneyValue.value — числовая сумма (НЕ amount).
    """
    buyer_id = 0
    if page.chat and page.chat.interlocutor:
        buyer_id = page.chat.interlocutor.id or 0
    price = None
    if page.order_total:
        price = float(page.order_total.value)
    return OrderInfo(
        order_id=page.order_id,
        status=_map_order_status(page.order_status),
        chat_id=int(page.chat.id) if page.chat and page.chat.id else 0,
        buyer_id=buyer_id,
        subcategory_id=page.order_subcategory_id,
        title=page.short_description,
        price=price,
    )


def _build_offer_info(preview) -> OfferInfo:
    """Сборка OfferInfo из OfferPreview funpaybotengine."""
    price = None
    if preview.price:
        price = float(preview.price.value)
    return OfferInfo(
        offer_id=int(preview.id),
        subcategory_id=0,
        title=preview.title,
        price=price,
        active=not preview.disabled,
        auto_delivery=preview.auto_delivery,
    )


class FunPayChatGateway:
    """Реализация ChatGateway поверх funpaybotengine.Bot.

    Bot注入ается в конструктор. Все методы делегируют к Bot с маппингом типов.
    """

    def __init__(self, bot) -> None:
        # bot — экземпляр funpaybotengine.Bot, тип не аннотируем строго
        # чтобы не тянуть зависимость в сигнатуру (тестируемость)
        self._bot = bot

    async def send_message(self, chat_id: int, text: str) -> int:
        msg = await self._bot.send_message(chat_id=chat_id, text=text)
        return msg.id if msg else 0

    async def get_order(self, order_id: str) -> OrderInfo:
        page = await self._bot.get_order_page(order_id=order_id)
        return _build_order_info(page)

    async def save_offer_fields(self, fields: OfferFieldsDTO) -> int:
        if fields.offer_id > 0:
            fp_fields = await self._bot.get_offer_fields(offer_id=fields.offer_id)
        else:
            fp_fields = await self._bot.get_offer_fields(subcategory_id=fields.subcategory_id)
        fp_fields.title_ru = fields.title_ru
        fp_fields.title_en = fields.title_en
        fp_fields.desc_ru = fields.desc_ru
        fp_fields.desc_en = fields.desc_en
        fp_fields.price = fields.price
        fp_fields.active = fields.active
        fp_fields.auto_delivery = fields.auto_delivery
        if fields.offer_id > 0:
            fp_fields.offer_id = fields.offer_id
        await self._bot.save_offer_fields(fp_fields)
        return fields.offer_id if fields.offer_id > 0 else 0

    async def set_offer_active(self, offer_id: int, active: bool) -> bool:
        fp_fields = await self._bot.get_offer_fields(offer_id=offer_id)
        fp_fields.active = active
        return await self._bot.save_offer_fields(fp_fields)

    async def get_my_offers(self, subcategory_id: int) -> list[OfferInfo]:
        page = await self._bot.get_my_offers_page(subcategory_id=subcategory_id)
        result = []
        for offer_id, preview in page.offers.items():
            info = _build_offer_info(preview)
            # subcategory_id не возвращается в preview, подставляем из запроса
            result.append(OfferInfo(
                offer_id=info.offer_id,
                subcategory_id=subcategory_id,
                title=info.title,
                price=info.price,
                active=info.active,
                auto_delivery=info.auto_delivery,
            ))
        return result

    async def bump_category(self, category_id: int, subcategory_id: int) -> bool:
        response = await self._bot.raise_offers(category_id, subcategory_id)
        return bool(response)
