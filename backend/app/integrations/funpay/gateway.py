from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from app.integrations.funpay.exceptions import FunPayApiError, FunPayOfferResolutionError
from app.integrations.funpay.provenance import (
    descriptions_have_exact_provenance,
    exact_provenance_token,
)
from app.integrations.funpay.types import (
    BuyerProfileInfo,
    OrderInfo,
    OfferInfo,
    OfferFieldsDTO,
    OfferSubscriptionOption,
    SalePreviewInfo,
    SalePreviewPage,
)


_PROVENANCE_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


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

    async def delete_offer(
        self,
        offer_id: int,
        *,
        expected_provenance_token: str,
    ) -> bool:
        """Delete one exact bot-owned offer and verify it disappeared."""
        ...

    async def get_my_offers(self, subcategory_id: int) -> list[OfferInfo]:
        """Получить список своих лотов в подкатегории."""
        ...

    async def get_offer_descriptions(
        self,
        offer_id: int,
    ) -> tuple[str | None, str | None]:
        """Return the full localized descriptions for provenance checks."""
        ...

    async def list_sales(
        self,
        *,
        limit: int = 100,
        order_id: str | None = None,
        cursor: str | None = None,
    ) -> SalePreviewPage:
        """Return recent sales only (never purchases)."""
        ...

    async def get_buyer_profile(self, buyer_id: int) -> BuyerProfileInfo:
        """Return one exact FunPay user profile by stable user ID."""
        ...

    async def get_category_id(self, subcategory_id: int) -> int | None:
        """Resolve parent category id needed by FunPay raise_offers()."""
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
        self._sales: list[SalePreviewInfo] = []
        self._sales_page_size = 100
        self.sales_list_calls: list[tuple[str | None, str | None, int]] = []
        self._buyer_profiles: dict[int, BuyerProfileInfo] = {}
        self.profile_calls: list[int] = []
        self.saved_offers: dict[int, OfferFieldsDTO] = {}
        self.activity_changes: list[tuple[int, bool]] = []
        self.deleted_offers: list[int] = []
        self.bumped: list[tuple[int, int]] = []
        self._my_offers: dict[int, list[OfferInfo]] = {}
        self._category_ids: dict[int, int] = {}
        self.offer_description_calls: list[int] = []

    def set_order(self, order: OrderInfo) -> None:
        self._orders[order.order_id] = order

    def set_sales(
        self,
        sales: list[SalePreviewInfo],
        *,
        page_size: int = 100,
    ) -> None:
        self._sales = list(sales)
        self._sales_page_size = max(1, page_size)

    def set_buyer_profile(self, profile: BuyerProfileInfo) -> None:
        self._buyer_profiles[profile.buyer_id] = profile

    def set_my_offers(self, subcategory_id: int, offers: list[OfferInfo]) -> None:
        self._my_offers[subcategory_id] = offers

    def set_offer_descriptions(
        self,
        offer_id: int,
        *,
        desc_ru: str | None,
        desc_en: str | None,
    ) -> None:
        existing = self.saved_offers.get(offer_id)
        if existing is None:
            self.saved_offers[offer_id] = OfferFieldsDTO(
                offer_id=offer_id,
                subcategory_id=0,
                title_ru="",
                title_en="",
                desc_ru=desc_ru or "",
                desc_en=desc_en or "",
                payment_msg_ru="",
                payment_msg_en="",
                subscription=OfferSubscriptionOption.WITHOUT_SUBSCRIPTION,
                subscription_type=None,
                price=0,
                amount=1,
                active=False,
                auto_delivery=False,
            )
            return
        self.saved_offers[offer_id] = OfferFieldsDTO(
            offer_id=existing.offer_id,
            subcategory_id=existing.subcategory_id,
            title_ru=existing.title_ru,
            title_en=existing.title_en,
            desc_ru=desc_ru or "",
            desc_en=desc_en or "",
            payment_msg_ru=existing.payment_msg_ru,
            payment_msg_en=existing.payment_msg_en,
            subscription=existing.subscription,
            subscription_type=existing.subscription_type,
            price=existing.price,
            amount=existing.amount,
            active=existing.active,
            auto_delivery=existing.auto_delivery,
        )

    def set_category_id(self, subcategory_id: int, category_id: int) -> None:
        self._category_ids[subcategory_id] = category_id

    async def send_message(self, chat_id: int, text: str) -> int:
        msg_id = self._next_message_id
        self._next_message_id += 1
        self.sent_messages.append((chat_id, text))
        return msg_id

    async def get_order(self, order_id: str) -> OrderInfo:
        if order_id not in self._orders:
            raise KeyError(order_id)
        return self._orders[order_id]

    async def list_sales(
        self,
        *,
        limit: int = 100,
        order_id: str | None = None,
        cursor: str | None = None,
    ) -> SalePreviewPage:
        self.sales_list_calls.append((cursor, order_id, limit))
        if order_id is not None:
            sales = tuple(item for item in self._sales if item.order_id == order_id)
            return SalePreviewPage(sales=sales[: max(0, limit)])
        start = 0
        if cursor is not None:
            start = next(
                (
                    index
                    for index, item in enumerate(self._sales)
                    if item.order_id == cursor
                ),
                len(self._sales),
            )
        page_size = min(max(0, limit), self._sales_page_size)
        end = min(len(self._sales), start + page_size)
        next_cursor = self._sales[end].order_id if end < len(self._sales) else None
        return SalePreviewPage(
            sales=tuple(self._sales[start:end]),
            next_cursor=next_cursor,
        )

    async def get_buyer_profile(self, buyer_id: int) -> BuyerProfileInfo:
        self.profile_calls.append(buyer_id)
        if buyer_id not in self._buyer_profiles:
            raise KeyError(buyer_id)
        return self._buyer_profiles[buyer_id]

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
                payment_msg_ru=fields.payment_msg_ru,
                payment_msg_en=fields.payment_msg_en,
                subscription=fields.subscription,
                subscription_type=fields.subscription_type,
                price=fields.price,
                amount=fields.amount,
                active=fields.active,
                auto_delivery=fields.auto_delivery,
            )
            self.saved_offers[offer_id] = updated
            return offer_id
        self.saved_offers[fields.offer_id] = fields
        return fields.offer_id

    async def set_offer_active(self, offer_id: int, active: bool) -> bool:
        self.activity_changes.append((offer_id, active))
        existing = self.saved_offers.get(offer_id)
        if existing is not None:
            self.saved_offers[offer_id] = replace(existing, active=active)
        return True

    async def delete_offer(
        self,
        offer_id: int,
        *,
        expected_provenance_token: str,
    ) -> bool:
        if not isinstance(expected_provenance_token, str) or not (
            _PROVENANCE_TOKEN_RE.fullmatch(expected_provenance_token)
        ):
            return False
        existing = self.saved_offers.get(offer_id)
        listed = any(
            any(item.offer_id == offer_id for item in offers)
            for offers in self._my_offers.values()
        )
        if existing is None and not listed:
            return False
        if existing is None or not descriptions_have_exact_provenance(
            (existing.desc_ru, existing.desc_en),
            expected_provenance_token,
        ):
            return False
        self.saved_offers.pop(offer_id, None)
        for subcategory_id, offers in tuple(self._my_offers.items()):
            self._my_offers[subcategory_id] = [
                item for item in offers if item.offer_id != offer_id
            ]
        self.deleted_offers.append(offer_id)
        return not any(
            any(item.offer_id == offer_id for item in offers)
            for offers in self._my_offers.values()
        )

    async def get_my_offers(self, subcategory_id: int) -> list[OfferInfo]:
        return self._my_offers.get(subcategory_id, [])

    async def get_offer_descriptions(
        self,
        offer_id: int,
    ) -> tuple[str | None, str | None]:
        self.offer_description_calls.append(offer_id)
        fields = self.saved_offers.get(offer_id)
        if fields is None:
            return None, None
        return fields.desc_ru, fields.desc_en

    async def get_category_id(self, subcategory_id: int) -> int | None:
        return self._category_ids.get(subcategory_id)

    async def bump_category(self, category_id: int, subcategory_id: int) -> bool:
        self.bumped.append((category_id, subcategory_id))
        return True


from funpayparsers.types.enums import OrderStatus as _FPOrderStatus

from app.integrations.funpay.types import SaleStatus


_CREATE_OFFER_RESOLUTION_DELAYS = (0.0, 0.25, 0.75, 1.5)
_DELETE_OFFER_VERIFICATION_DELAYS = (0.0, 0.25, 0.75, 1.5)


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
    buyer = page.chat.interlocutor if page.chat and page.chat.interlocutor else None
    if buyer:
        buyer_id = buyer.id or 0
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
        full_description=getattr(page, "full_description", None),
        offer_id=_extract_order_offer_id(page),
        buyer_username=getattr(buyer, "username", None),
        buyer_avatar_url=getattr(buyer, "avatar_url", None),
        buyer_is_online=getattr(buyer, "online", None),
        buyer_status_text=getattr(buyer, "status_text", None),
    )


def _build_sale_preview_info(preview) -> SalePreviewInfo:
    buyer = preview.counterparty
    timestamp = preview.timestamp
    created_at = (
        datetime.fromtimestamp(timestamp, timezone.utc) if timestamp > 0 else None
    )
    return SalePreviewInfo(
        order_id=str(preview.id),
        status=_map_order_status(preview.status),
        buyer_id=int(buyer.id),
        buyer_username=buyer.username or None,
        buyer_avatar_url=buyer.avatar_url or None,
        buyer_is_online=buyer.online,
        buyer_status_text=buyer.status_text or None,
        created_at=created_at,
    )


def _extract_order_offer_id(page) -> int | None:
    """Best-effort extraction of an offer id from parser/vendor extensions.

    FunPayBotEngine 0.7 does not expose an offer id on ``OrderPage``.  Some
    patched parsers and page payloads do, so the adapter accepts those values
    without making the domain depend on their exact shape.
    """
    for attr in ("offer_id", "order_offer_id"):
        value = getattr(page, attr, None)
        parsed = _positive_int(value)
        if parsed is not None:
            return parsed

    data = getattr(page, "data", None)
    if isinstance(data, Mapping):
        normalized = {str(key).strip().lower(): value for key, value in data.items()}
        for key in ("offer_id", "offer id", "id лота", "id предложения"):
            parsed = _positive_int(normalized.get(key))
            if parsed is not None:
                return parsed

        # A patched parser may retain a canonical offer URL in the order data.
        for value in normalized.values():
            match = re.search(r"(?:offer\?id=|offer/)(\d+)", str(value))
            if match:
                return int(match.group(1))
    return None


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clean_optional_text(value) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


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
        if msg is None:
            # Callers persist buyer-visible delivery markers immediately after
            # this method returns. A missing message object is not a confirmed
            # send and must therefore enter the normal retry/manual path.
            raise FunPayApiError(0, "send_message returned no message")
        return int(msg.id)

    async def get_order(self, order_id: str) -> OrderInfo:
        page = await self._bot.get_order_page(order_id=order_id)
        return _build_order_info(page)

    async def list_sales(
        self,
        *,
        limit: int = 100,
        order_id: str | None = None,
        cursor: str | None = None,
    ) -> SalePreviewPage:
        if limit <= 0:
            return SalePreviewPage(sales=())
        batch = await self._bot.get_sales(
            from_order_id=cursor,
            order_id_filter=order_id,
        )
        orders = (
            [item for item in batch.orders if str(item.id) == order_id]
            if order_id is not None
            else list(batch.orders)
        )
        next_cursor = None
        if order_id is None:
            next_cursor = (
                str(orders[limit].id) if len(orders) > limit else batch.next_order_id
            )
        return SalePreviewPage(
            sales=tuple(_build_sale_preview_info(item) for item in orders[:limit]),
            next_cursor=next_cursor,
        )

    async def get_buyer_profile(self, buyer_id: int) -> BuyerProfileInfo:
        page = await self._bot.get_profile_page(id=buyer_id)
        return BuyerProfileInfo(
            buyer_id=int(page.user_id),
            username=_clean_optional_text(page.username),
            avatar_url=_clean_optional_text(page.avatar_url),
            is_online=bool(page.online),
            status_text=_clean_optional_text(page.status_text),
        )

    async def save_offer_fields(self, fields: OfferFieldsDTO) -> int:
        if self._bot is None:
            raise RuntimeError("FunPayChatGateway requires a bound Bot")

        # FunPayBotEngine 0.7 requires the subcategory namespace together
        # with its numeric ID when fields for a new offer are requested.
        # Passing only ``subcategory_id`` leaves the bot connected but makes
        # every automatic lot creation fail before the first save request.
        from funpaybotengine.types.enums import SubcategoryType

        before: list[OfferInfo] = []
        if fields.offer_id <= 0:
            before = await self.get_my_offers(fields.subcategory_id)

        if fields.offer_id > 0:
            fp_fields = await self._bot.get_offer_fields(offer_id=fields.offer_id)
        else:
            fp_fields = await self._bot.get_offer_fields(
                subcategory_type=SubcategoryType.OFFERS,
                subcategory_id=fields.subcategory_id,
            )
        fp_fields.title_ru = fields.title_ru
        fp_fields.title_en = fields.title_en
        fp_fields.desc_ru = fields.desc_ru
        fp_fields.desc_en = fields.desc_en
        fp_fields.payment_msg_ru = fields.payment_msg_ru
        fp_fields.payment_msg_en = fields.payment_msg_en
        fp_fields.set_field("fields[subscription]", fields.subscription.value)
        fp_fields.set_field(
            "fields[type]",
            fields.subscription_type.value if fields.subscription_type else "",
        )
        fp_fields.price = fields.price
        fp_fields.amount = fields.amount
        fp_fields.active = fields.active
        fp_fields.auto_delivery = fields.auto_delivery
        if fields.offer_id > 0:
            fp_fields.offer_id = fields.offer_id
        saved = await self._bot.save_offer_fields(fp_fields)
        if not saved:
            raise FunPayApiError(0, "save_offer_fields returned false")
        if fields.offer_id > 0:
            return fields.offer_id

        for delay in _CREATE_OFFER_RESOLUTION_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            after = await self.get_my_offers(fields.subcategory_id)
            resolved = await _resolve_created_offer_id(
                before,
                after,
                fields,
                self.get_offer_descriptions,
            )
            if resolved is not None:
                return resolved
        raise FunPayOfferResolutionError(
            "FunPay accepted a new offer but its exact bot marker was not "
            "visible in the seller offer list"
        )

    async def set_offer_active(self, offer_id: int, active: bool) -> bool:
        fp_fields = await self._bot.get_offer_fields(offer_id=offer_id)
        fp_fields.active = active
        return await self._bot.save_offer_fields(fp_fields)

    async def delete_offer(
        self,
        offer_id: int,
        *,
        expected_provenance_token: str,
    ) -> bool:
        """Delete through FunPay's own offer form and verify the postcondition.

        FunPayBotEngine 0.7 has no dedicated delete method. The official form
        submits the freshly loaded fields to ``lots/offerSave`` with the
        hidden ``deleted`` field set to ``1``. Reusing that exact form keeps
        CSRF and ``form_created_at`` handling inside the engine.
        """

        if not isinstance(expected_provenance_token, str) or not (
            _PROVENANCE_TOKEN_RE.fullmatch(expected_provenance_token)
        ):
            return False

        fp_fields = await self._bot.get_offer_fields(offer_id=offer_id)
        parsed_offer_id = _positive_int(getattr(fp_fields, "offer_id", None))
        subcategory_id = _positive_int(
            getattr(fp_fields, "subcategory_id", None)
        )
        if parsed_offer_id != offer_id or subcategory_id is None:
            return False
        descriptions = (
            _clean_optional_text(getattr(fp_fields, "desc_ru", None)),
            _clean_optional_text(getattr(fp_fields, "desc_en", None)),
        )
        if not descriptions_have_exact_provenance(
            descriptions,
            expected_provenance_token,
        ):
            return False
        fp_fields.set_field("deleted", "1")
        if not await self._bot.save_offer_fields(fp_fields):
            return False
        for delay in _DELETE_OFFER_VERIFICATION_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            page = await self._bot.get_my_offers_page(
                subcategory_id=subcategory_id
            )
            # The vendor parser returns an empty offer set with node ``0``
            # for unrelated/login/error HTML. Such a page must never prove
            # deletion merely because the target ID is absent.
            if _positive_int(getattr(page, "subcategory_id", None)) != (
                subcategory_id
            ):
                continue
            visible_ids = {
                parsed
                for raw_id in page.offers
                if (parsed := _positive_int(raw_id)) is not None
            }
            if offer_id not in visible_ids:
                return True
        return False

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

    async def get_offer_descriptions(
        self,
        offer_id: int,
    ) -> tuple[str | None, str | None]:
        fields = await self._bot.get_offer_fields(offer_id=offer_id)
        return (
            _clean_optional_text(getattr(fields, "desc_ru", None)),
            _clean_optional_text(getattr(fields, "desc_en", None)),
        )

    async def get_category_id(self, subcategory_id: int) -> int | None:
        page = await self._bot.get_my_offers_page(subcategory_id=subcategory_id)
        return _positive_int(page.category_id)

    async def bump_category(self, category_id: int, subcategory_id: int) -> bool:
        response = await self._bot.raise_offers(category_id, subcategory_id)
        return bool(response)


async def _resolve_created_offer_id(
    before: Iterable[OfferInfo],
    after: Iterable[OfferInfo],
    requested: OfferFieldsDTO,
    get_descriptions: Callable[
        [int],
        Awaitable[tuple[str | None, str | None]],
    ],
) -> int | None:
    """Resolve the ID created by FunPayBotEngine 0.7.

    ``Bot.save_offer_fields`` returns only a boolean and FunPay decorates the
    preview title with form attributes. Snapshot difference bounds the
    candidates; only the exact immutable marker in the full descriptions can
    identify the created bot offer. Concurrent manual offers are never
    adopted, even if their title and price are identical.
    """
    requested_token = exact_provenance_token(
        (requested.desc_ru, requested.desc_en)
    )
    if requested_token is None:
        raise FunPayOfferResolutionError(
            "A new bot offer requires one exact provenance marker"
        )
    before_ids = {item.offer_id for item in before}
    new_items = [item for item in after if item.offer_id not in before_ids]
    requested_titles = {
        _normalize_offer_title(requested.title_ru),
        _normalize_offer_title(requested.title_en),
    }
    requested_titles.discard("")
    # Real previews append values such as ``, Без подписки``. Prefix
    # similarity only prioritizes network reads and never proves ownership.
    new_items.sort(key=lambda item: not any(
        _normalize_offer_title(item.title).startswith(title)
        for title in requested_titles
    ))
    matching: list[OfferInfo] = []
    description_failed = False
    for item in new_items:
        try:
            descriptions = await get_descriptions(item.offer_id)
        except Exception:
            description_failed = True
            continue
        if descriptions_have_exact_provenance(
            descriptions,
            requested_token,
        ):
            matching.append(item)
    # Do not bind while any new candidate is uninspectable: it could be a
    # second copy of the same bot offer. A bounded outer retry handles normal
    # eventual consistency and transient form reads.
    if description_failed:
        return None
    if len(matching) == 1:
        return matching[0].offer_id
    if len(matching) > 1:
        raise FunPayOfferResolutionError(
            "More than one new FunPay offer carries the requested bot marker"
        )
    return None


def _normalize_offer_title(value: str | None) -> str:
    return " ".join((value or "").casefold().split())
