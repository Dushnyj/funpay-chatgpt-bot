from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SaleStatus(str, Enum):
    """Статусы заказа FunPay (маппинг OrderStatus из funpayparsers)."""

    PAID = "paid"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    UNKNOWN = "unknown"


class OfferSubscriptionOption(str, Enum):
    """Exact values accepted by FunPay's ChatGPT subscription selector."""

    WITH_SUBSCRIPTION = "С подпиской"
    WITHOUT_SUBSCRIPTION = "Без подписки"


class OfferSubscriptionType(str, Enum):
    """Exact conditional values accepted by FunPay's plan selector."""

    GO = "Go"
    PLUS = "Plus"
    PRO = "Pro"
    BUSINESS = "Business"


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
    # Immutable detailed description copied into the FunPay order page.  The
    # bot provenance marker lives here when the stock parser omits offer_id.
    full_description: str | None = None
    # FunPayBotEngine 0.7 OrderPage does not currently expose this field, but
    # keeping it in the boundary DTO lets adapters use it when FunPay adds it
    # (or when another parser can recover it). Domain matching prefers this
    # stable remote identifier and otherwise requires the description marker.
    offer_id: int | None = None
    buyer_username: str | None = None
    buyer_avatar_url: str | None = None
    buyer_is_online: bool | None = None
    buyer_status_text: str | None = None


@dataclass(frozen=True)
class SalePreviewInfo:
    """Sale-only preview returned by ``Bot.get_sales()``."""

    order_id: str
    status: SaleStatus
    buyer_id: int
    buyer_username: str | None
    buyer_avatar_url: str | None
    buyer_is_online: bool | None
    buyer_status_text: str | None
    created_at: datetime | None = None


@dataclass(frozen=True)
class SalePreviewPage:
    """One bounded page from the sale-only order history."""

    sales: tuple[SalePreviewInfo, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class BuyerProfileInfo:
    """Authoritative buyer profile returned by ``Bot.get_profile_page()``."""

    buyer_id: int
    username: str | None
    avatar_url: str | None
    is_online: bool | None
    status_text: str | None


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
    from_me: bool = False
    sender_username: str | None = None
    buyer_id: int | None = None
    buyer_username: str | None = None
    seller_id: int | None = None
    seller_username: str | None = None


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
    payment_msg_ru: str
    payment_msg_en: str
    subscription: OfferSubscriptionOption
    subscription_type: OfferSubscriptionType | None
    price: float
    amount: int
    active: bool
    auto_delivery: bool
