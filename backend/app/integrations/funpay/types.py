from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SaleStatus(str, Enum):
    """Статусы заказа FunPay (маппинг OrderStatus из funpayparsers)."""

    PAID = "paid"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OrderInfo:
    """DTO заказа FunPay, результат gateway.get_order()."""

    order_id: str
    status: SaleStatus
    chat_id: int
    buyer_id: int
    subcategory_id: int
    title: str | None
    price: float | None


@dataclass(frozen=True)
class OfferInfo:
    """DTO существующего лота (оффера) на FunPay."""

    offer_id: int
    subcategory_id: int
    title: str | None
    price: float | None
    active: bool
    auto_delivery: bool


@dataclass(frozen=True)
class MessageInfo:
    """DTO сообщения из чата FunPay."""

    message_id: int
    chat_id: int
    sender_id: int | None
    text: str | None
    order_id: str | None


@dataclass(frozen=True)
class OfferFieldsDTO:
    """DTO полей лота для создания/обновления через gateway.save_offer_fields().

    offer_id=0 означает создание нового лота.
    """

    offer_id: int
    subcategory_id: int
    title_ru: str
    title_en: str
    desc_ru: str
    desc_en: str
    price: float
    active: bool
    auto_delivery: bool
