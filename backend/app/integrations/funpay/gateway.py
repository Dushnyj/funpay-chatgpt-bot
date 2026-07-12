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
