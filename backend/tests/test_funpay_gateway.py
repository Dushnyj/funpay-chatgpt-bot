import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.integrations.funpay.exceptions import FunPayApiError, FunPayOfferResolutionError
from app.integrations.funpay.gateway import FakeChatGateway, FunPayChatGateway
from app.integrations.funpay.types import (
    OrderInfo,
    OfferInfo,
    SaleStatus,
    OfferFieldsDTO,
)


@pytest.fixture
def gw() -> FakeChatGateway:
    return FakeChatGateway()


async def test_send_message_records_call(gw: FakeChatGateway):
    msg_id = await gw.send_message(chat_id=100, text="hello")
    assert msg_id > 0
    assert (100, "hello") in gw.sent_messages


async def test_get_order_returns_set_order(gw: FakeChatGateway):
    order = OrderInfo(
        order_id="42",
        status=SaleStatus.PAID,
        chat_id=10,
        buyer_id=5,
        subcategory_id=55,
        title="test",
        price=100.0,
    )
    gw.set_order(order)
    result = await gw.get_order("42")
    assert result is order


async def test_get_order_not_found_raises(gw: FakeChatGateway):
    with pytest.raises(KeyError):
        await gw.get_order("nonexistent")


async def test_save_offer_returns_new_id_and_records(gw: FakeChatGateway):
    fields = OfferFieldsDTO(
        offer_id=0,
        subcategory_id=55,
        title_ru="T",
        title_en="T",
        desc_ru="",
        desc_en="",
        price=100.0,
        active=True,
        auto_delivery=False,
    )
    new_id = await gw.save_offer_fields(fields)
    assert new_id > 0
    assert new_id in gw.saved_offers
    saved = gw.saved_offers[new_id]
    assert saved.title_ru == "T"
    assert saved.offer_id == new_id


async def test_save_offer_updates_existing(gw: FakeChatGateway):
    fields = OfferFieldsDTO(
        offer_id=0,
        subcategory_id=55,
        title_ru="Old",
        title_en="Old",
        desc_ru="",
        desc_en="",
        price=100.0,
        active=True,
        auto_delivery=False,
    )
    new_id = await gw.save_offer_fields(fields)

    updated = OfferFieldsDTO(
        offer_id=new_id,
        subcategory_id=55,
        title_ru="New",
        title_en="New",
        desc_ru="",
        desc_en="",
        price=200.0,
        active=False,
        auto_delivery=False,
    )
    same_id = await gw.save_offer_fields(updated)
    assert same_id == new_id
    assert gw.saved_offers[new_id].title_ru == "New"
    assert gw.saved_offers[new_id].active is False


async def test_bump_category_returns_true_records(gw: FakeChatGateway):
    result = await gw.bump_category(category_id=1, subcategory_id=55)
    assert result is True
    assert (1, 55) in gw.bumped


async def test_set_offer_active_records(gw: FakeChatGateway):
    await gw.set_offer_active(offer_id=10, active=False)
    assert (10, False) in gw.activity_changes


async def test_get_my_offers_returns_set(gw: FakeChatGateway):
    offer = OfferInfo(
        offer_id=10,
        subcategory_id=55,
        title="X",
        price=100.0,
        active=True,
        auto_delivery=False,
    )
    gw.set_my_offers(55, [offer])
    result = await gw.get_my_offers(subcategory_id=55)
    assert result == [offer]


def _preview(offer_id: int, title: str, price: float):
    return SimpleNamespace(
        id=offer_id,
        title=title,
        price=SimpleNamespace(value=price),
        disabled=False,
        auto_delivery=False,
    )


async def test_engine_07_create_resolves_new_offer_id_from_snapshots():
    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(side_effect=[
            SimpleNamespace(offers={10: _preview(10, "Old", 10)}),
            SimpleNamespace(offers={
                10: _preview(10, "Old", 10),
                42: _preview(42, "Target", 599),
            }),
        ]),
        get_offer_fields=AsyncMock(return_value=SimpleNamespace()),
        save_offer_fields=AsyncMock(return_value=True),
    )
    fields = OfferFieldsDTO(
        offer_id=0, subcategory_id=55, title_ru="Target", title_en="Target",
        desc_ru="", desc_en="", price=599, active=True, auto_delivery=False,
    )

    assert await FunPayChatGateway(bot).save_offer_fields(fields) == 42
    bot.save_offer_fields.assert_awaited_once()


async def test_engine_07_create_rejects_ambiguous_offer_id():
    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(side_effect=[
            SimpleNamespace(offers={}),
            SimpleNamespace(offers={
                41: _preview(41, "Target", 599),
                42: _preview(42, "Target", 599),
            }),
        ]),
        get_offer_fields=AsyncMock(return_value=SimpleNamespace()),
        save_offer_fields=AsyncMock(return_value=True),
    )
    fields = OfferFieldsDTO(
        offer_id=0, subcategory_id=55, title_ru="Target", title_en="Target",
        desc_ru="", desc_en="", price=599, active=True, auto_delivery=False,
    )

    with pytest.raises(FunPayOfferResolutionError):
        await FunPayChatGateway(bot).save_offer_fields(fields)


async def test_engine_07_create_rejects_unrelated_sole_delta():
    fields = OfferFieldsDTO(
        offer_id=0,
        subcategory_id=55,
        title_ru="Нужный лот",
        title_en="Expected lot",
        desc_ru="",
        desc_en="",
        price=199,
        active=True,
        auto_delivery=False,
    )
    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(side_effect=[
            SimpleNamespace(offers={}),
            SimpleNamespace(offers={
                77: _preview(77, "Unrelated concurrent offer", 1),
            }),
        ]),
        get_offer_fields=AsyncMock(return_value=SimpleNamespace()),
        save_offer_fields=AsyncMock(return_value=True),
    )

    with pytest.raises(FunPayOfferResolutionError):
        await FunPayChatGateway(bot).save_offer_fields(fields)


async def test_engine_07_false_save_result_is_an_api_error():
    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(return_value=SimpleNamespace(offers={})),
        get_offer_fields=AsyncMock(return_value=SimpleNamespace()),
        save_offer_fields=AsyncMock(return_value=False),
    )
    fields = OfferFieldsDTO(
        offer_id=0, subcategory_id=55, title_ru="Target", title_en="Target",
        desc_ru="", desc_en="", price=599, active=True, auto_delivery=False,
    )
    with pytest.raises(FunPayApiError):
        await FunPayChatGateway(bot).save_offer_fields(fields)


from app.integrations.funpay.gateway import (
    _map_order_status,
    _build_order_info,
    _build_offer_info,
)
from app.integrations.funpay.types import SaleStatus


def test_map_order_status_paid():
    from funpayparsers.types.enums import OrderStatus as FPOrderStatus
    assert _map_order_status(FPOrderStatus.PAID) is SaleStatus.PAID
    assert _map_order_status(FPOrderStatus.COMPLETED) is SaleStatus.COMPLETED
    assert _map_order_status(FPOrderStatus.REFUNDED) is SaleStatus.REFUNDED


def test_map_order_status_unknown_default():
    from funpayparsers.types.enums import OrderStatus as FPOrderStatus
    assert _map_order_status(FPOrderStatus.UNKNOWN) is SaleStatus.UNKNOWN
