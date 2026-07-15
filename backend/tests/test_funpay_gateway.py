import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.integrations.funpay.exceptions import FunPayApiError, FunPayOfferResolutionError
from app.integrations.funpay.gateway import FakeChatGateway, FunPayChatGateway
from app.integrations.funpay.provenance import public_provenance_code
from app.integrations.funpay.types import (
    BuyerProfileInfo,
    OrderInfo,
    OfferInfo,
    SaleStatus,
    OfferFieldsDTO,
    OfferSubscriptionOption,
    OfferSubscriptionType,
)


def _offer_fields(**overrides) -> OfferFieldsDTO:
    marker = "[FPBOT:0123456789abcdef0123456789abcdef]"
    values = {
        "offer_id": 0,
        "subcategory_id": 55,
        "title_ru": "Target",
        "title_en": "Target",
        "desc_ru": f"Описание\n\n{marker}",
        "desc_en": f"Description\n\n{marker}",
        "payment_msg_ru": "Заказ принят.",
        "payment_msg_en": "Order accepted.",
        "subscription": OfferSubscriptionOption.WITH_SUBSCRIPTION,
        "subscription_type": OfferSubscriptionType.PLUS,
        "price": 599,
        "amount": 1,
        "active": True,
        "auto_delivery": False,
    }
    values.update(overrides)
    return OfferFieldsDTO(**values)


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


async def test_profile_gateway_maps_exact_user_and_normalizes_text():
    page = SimpleNamespace(
        user_id="42",
        username="  buyer-42  ",
        avatar_url="   ",
        online=0,
        status_text="  был недавно  ",
    )
    bot = SimpleNamespace(get_profile_page=AsyncMock(return_value=page))

    profile = await FunPayChatGateway(bot).get_buyer_profile(42)

    assert profile == BuyerProfileInfo(
        buyer_id=42,
        username="buyer-42",
        avatar_url=None,
        is_online=False,
        status_text="был недавно",
    )
    bot.get_profile_page.assert_awaited_once_with(id=42)


async def test_save_offer_returns_new_id_and_records(gw: FakeChatGateway):
    fields = _offer_fields(title_ru="T", title_en="T", price=100.0)
    new_id = await gw.save_offer_fields(fields)
    assert new_id > 0
    assert new_id in gw.saved_offers
    saved = gw.saved_offers[new_id]
    assert saved.title_ru == "T"
    assert saved.offer_id == new_id


async def test_save_offer_updates_existing(gw: FakeChatGateway):
    fields = _offer_fields(title_ru="Old", title_en="Old", price=100.0)
    new_id = await gw.save_offer_fields(fields)

    updated = _offer_fields(
        offer_id=new_id,
        title_ru="New",
        title_en="New",
        price=200.0,
        active=False,
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


async def test_fake_delete_offer_requires_expected_marker(gw: FakeChatGateway):
    fields = _offer_fields(offer_id=10, active=False)
    await gw.save_offer_fields(fields)
    gw.set_my_offers(55, [
        OfferInfo(10, 55, "Target", 599, False, False),
    ])

    assert not await gw.delete_offer(
        10,
        expected_provenance_token="f" * 32,
    )
    assert 10 in gw.saved_offers
    assert await gw.delete_offer(
        10,
        expected_provenance_token="0123456789abcdef0123456789abcdef",
    )
    assert 10 not in gw.saved_offers
    assert await gw.get_my_offers(55) == []
    assert gw.deleted_offers == [10]


async def test_fake_delete_offer_rejects_extended_public_code(
    gw: FakeChatGateway,
):
    token = "0123456789abcdef0123456789abcdef"
    malformed = f"{public_provenance_code(token)}A"
    fields = _offer_fields(
        offer_id=10,
        active=False,
        desc_ru=f"Описание\n\n{malformed}",
        desc_en=f"Description\n\n{malformed}",
    )
    await gw.save_offer_fields(fields)
    gw.set_my_offers(55, [
        OfferInfo(10, 55, "Target", 599, False, False),
    ])

    assert not await gw.delete_offer(
        10,
        expected_provenance_token=token,
    )
    assert 10 in gw.saved_offers
    assert gw.deleted_offers == []


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


def _engine_offer_fields():
    """Engine 0.7 field object shaped like the live node 1355 form."""
    from funpaybotengine.types import OfferFields

    return OfferFields(
        raw_source="",
        fields_dict={
            "form_created_at": "1783991480",
            "offer_id": "0",
            "node_id": "55",
            "location": "",
            "deleted": "",
            "fields[subscription]": "",
            "fields[type]": "",
            "fields[summary][ru]": "",
            "fields[summary][en]": "",
            "fields[desc][ru]": "",
            "fields[desc][en]": "",
            "fields[payment_msg][ru]": "",
            "fields[payment_msg][en]": "",
            "fields[images]": "",
            "auto_delivery": "",
            "secrets": "",
            "price": "",
            "amount": "",
            "active": "on",
        },
    )


async def test_engine_07_create_resolves_new_offer_id_from_snapshots():
    from funpaybotengine.types.enums import SubcategoryType

    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(side_effect=[
            SimpleNamespace(offers={10: _preview(10, "Old", 10)}),
            SimpleNamespace(offers={
                10: _preview(10, "Old", 10),
                42: _preview(42, "Target, С подпиской", 599),
            }),
        ]),
        get_offer_fields=AsyncMock(return_value=_engine_offer_fields()),
        save_offer_fields=AsyncMock(return_value=True),
    )
    fields = _offer_fields()

    assert await FunPayChatGateway(bot).save_offer_fields(fields) == 42
    bot.save_offer_fields.assert_awaited_once()
    bot.get_offer_fields.assert_any_await(
        subcategory_type=SubcategoryType.OFFERS,
        subcategory_id=55,
    )
    bot.get_offer_fields.assert_any_await(offer_id=42)
    submitted = bot.save_offer_fields.await_args.args[0].fields_dict
    assert submitted["fields[subscription]"] == "С подпиской"
    assert submitted["fields[type]"] == "Plus"
    assert submitted["fields[summary][ru]"] == "Target"
    assert submitted["fields[summary][en]"] == "Target"
    assert submitted["fields[desc][ru]"] == fields.desc_ru
    assert submitted["fields[desc][en]"] == fields.desc_en
    assert submitted["fields[payment_msg][ru]"] == "Заказ принят."
    assert submitted["fields[payment_msg][en]"] == "Order accepted."
    assert submitted["price"] == "599"
    assert submitted["amount"] == "1"
    assert submitted["auto_delivery"] == ""
    assert submitted["active"] == "on"


async def test_engine_07_free_offer_clears_conditional_subscription_type():
    fp_fields = _engine_offer_fields()
    fp_fields.set_field("fields[type]", "Pro")
    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(side_effect=[
            SimpleNamespace(offers={}),
            SimpleNamespace(offers={42: _preview(42, "Target", 599)}),
        ]),
        get_offer_fields=AsyncMock(return_value=fp_fields),
        save_offer_fields=AsyncMock(return_value=True),
    )
    fields = _offer_fields(
        subscription=OfferSubscriptionOption.WITHOUT_SUBSCRIPTION,
        subscription_type=None,
    )

    assert await FunPayChatGateway(bot).save_offer_fields(fields) == 42

    submitted = bot.save_offer_fields.await_args.args[0].fields_dict
    assert submitted["fields[subscription]"] == "Без подписки"
    assert submitted["fields[type]"] == ""


async def test_engine_07_create_rejects_ambiguous_offer_id():
    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(side_effect=[
            SimpleNamespace(offers={}),
            SimpleNamespace(offers={
                41: _preview(41, "Target", 599),
                42: _preview(42, "Target", 599),
            }),
        ]),
        get_offer_fields=AsyncMock(return_value=_engine_offer_fields()),
        save_offer_fields=AsyncMock(return_value=True),
    )
    fields = _offer_fields()

    with pytest.raises(FunPayOfferResolutionError):
        await FunPayChatGateway(bot).save_offer_fields(fields)


async def test_engine_07_create_never_adopts_manual_lookalike(
    monkeypatch,
):
    import app.integrations.funpay.gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "_CREATE_OFFER_RESOLUTION_DELAYS",
        (0.0, 0.0, 0.0, 0.0),
    )
    fields = _offer_fields(
        title_ru="Нужный лот",
        title_en="Expected lot",
        price=199,
    )
    create_fields = _engine_offer_fields()

    async def get_fields(*, offer_id=None, **_kwargs):
        if offer_id is None:
            return create_fields
        return SimpleNamespace(
            desc_ru="Ручной лот продавца",
            desc_en="Seller's manual offer",
        )

    before = SimpleNamespace(offers={})
    after = SimpleNamespace(offers={
        77: _preview(77, "Нужный лот, Plus", 199),
    })
    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(
            side_effect=[before, after, after, after, after],
        ),
        get_offer_fields=AsyncMock(side_effect=get_fields),
        save_offer_fields=AsyncMock(return_value=True),
    )

    with pytest.raises(FunPayOfferResolutionError):
        await FunPayChatGateway(bot).save_offer_fields(fields)


async def test_engine_07_chooses_marker_over_concurrent_manual_lookalike():
    fields = _offer_fields()
    create_fields = _engine_offer_fields()

    async def get_fields(*, offer_id=None, **_kwargs):
        if offer_id is None:
            return create_fields
        if offer_id == 41:
            return SimpleNamespace(desc_ru="Manual", desc_en="Manual")
        return SimpleNamespace(desc_ru=fields.desc_ru, desc_en=fields.desc_en)

    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(side_effect=[
            SimpleNamespace(offers={}),
            SimpleNamespace(offers={
                41: _preview(41, "Target, Plus", 599),
                42: _preview(42, "Target, Plus", 599),
            }),
        ]),
        get_offer_fields=AsyncMock(side_effect=get_fields),
        save_offer_fields=AsyncMock(return_value=True),
    )

    assert await FunPayChatGateway(bot).save_offer_fields(fields) == 42


async def test_engine_07_retries_eventually_consistent_offer_list(monkeypatch):
    import app.integrations.funpay.gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "_CREATE_OFFER_RESOLUTION_DELAYS",
        (0.0, 0.0),
    )
    fields = _offer_fields()
    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(side_effect=[
            SimpleNamespace(offers={}),
            SimpleNamespace(offers={}),
            SimpleNamespace(offers={
                42: _preview(42, "Target, Plus", 599),
            }),
        ]),
        get_offer_fields=AsyncMock(return_value=_engine_offer_fields()),
        save_offer_fields=AsyncMock(return_value=True),
    )

    assert await FunPayChatGateway(bot).save_offer_fields(fields) == 42


async def test_engine_07_false_save_result_is_an_api_error():
    bot = SimpleNamespace(
        get_my_offers_page=AsyncMock(return_value=SimpleNamespace(offers={})),
        get_offer_fields=AsyncMock(return_value=_engine_offer_fields()),
        save_offer_fields=AsyncMock(return_value=False),
    )
    fields = _offer_fields()
    with pytest.raises(FunPayApiError):
        await FunPayChatGateway(bot).save_offer_fields(fields)


async def test_engine_07_deletes_offer_and_verifies_absence(monkeypatch):
    import app.integrations.funpay.gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "_DELETE_OFFER_VERIFICATION_DELAYS",
        (0.0, 0.0),
    )
    fields = _engine_offer_fields()
    fields.offer_id = 77
    fields.subcategory_id = 55
    fields.desc_ru = "Описание\n\n[FPBOT:0123456789abcdef0123456789abcdef]"
    fields.desc_en = "Description\n\n[FPBOT:0123456789abcdef0123456789abcdef]"
    bot = SimpleNamespace(
        get_offer_fields=AsyncMock(return_value=fields),
        save_offer_fields=AsyncMock(return_value=True),
        get_my_offers_page=AsyncMock(side_effect=[
            SimpleNamespace(subcategory_id=55, offers={77: object()}),
            SimpleNamespace(subcategory_id=55, offers={}),
        ]),
    )

    deleted = await FunPayChatGateway(bot).delete_offer(
        77,
        expected_provenance_token="0123456789abcdef0123456789abcdef",
    )

    assert deleted is True
    submitted = bot.save_offer_fields.await_args.args[0]
    assert submitted.fields_dict["deleted"] == "1"
    bot.get_offer_fields.assert_awaited_once_with(offer_id=77)
    assert bot.get_my_offers_page.await_count == 2
    bot.get_my_offers_page.assert_awaited_with(subcategory_id=55)


async def test_engine_07_delete_fails_closed_on_marker_mismatch():
    fields = _engine_offer_fields()
    fields.offer_id = 77
    fields.subcategory_id = 55
    fields.desc_ru = "[FPBOT:ffffffffffffffffffffffffffffffff]"
    fields.desc_en = "[FPBOT:ffffffffffffffffffffffffffffffff]"
    bot = SimpleNamespace(
        get_offer_fields=AsyncMock(return_value=fields),
        save_offer_fields=AsyncMock(return_value=True),
        get_my_offers_page=AsyncMock(),
    )

    deleted = await FunPayChatGateway(bot).delete_offer(
        77,
        expected_provenance_token="0123456789abcdef0123456789abcdef",
    )

    assert deleted is False
    bot.save_offer_fields.assert_not_awaited()
    bot.get_my_offers_page.assert_not_awaited()


async def test_engine_07_delete_rejects_invalid_token_before_remote_read():
    bot = SimpleNamespace(
        get_offer_fields=AsyncMock(),
        save_offer_fields=AsyncMock(),
        get_my_offers_page=AsyncMock(),
    )

    assert await FunPayChatGateway(bot).delete_offer(
        77,
        expected_provenance_token="not-a-token",
    ) is False
    bot.get_offer_fields.assert_not_awaited()
    bot.save_offer_fields.assert_not_awaited()
    bot.get_my_offers_page.assert_not_awaited()


async def test_engine_07_delete_fails_closed_on_offer_id_mismatch():
    fields = _engine_offer_fields()
    fields.offer_id = 78
    fields.subcategory_id = 55
    bot = SimpleNamespace(
        get_offer_fields=AsyncMock(return_value=fields),
        save_offer_fields=AsyncMock(return_value=True),
        get_my_offers_page=AsyncMock(),
    )

    assert await FunPayChatGateway(bot).delete_offer(
        77,
        expected_provenance_token="0123456789abcdef0123456789abcdef",
    ) is False
    bot.save_offer_fields.assert_not_awaited()
    bot.get_my_offers_page.assert_not_awaited()


async def test_engine_07_delete_requires_verified_absence(monkeypatch):
    import app.integrations.funpay.gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "_DELETE_OFFER_VERIFICATION_DELAYS",
        (0.0, 0.0),
    )
    fields = _engine_offer_fields()
    fields.offer_id = 77
    fields.subcategory_id = 55
    fields.desc_ru = "[FPBOT:0123456789abcdef0123456789abcdef]"
    fields.desc_en = "[FPBOT:0123456789abcdef0123456789abcdef]"
    bot = SimpleNamespace(
        get_offer_fields=AsyncMock(return_value=fields),
        save_offer_fields=AsyncMock(return_value=True),
        get_my_offers_page=AsyncMock(
            return_value=SimpleNamespace(
                subcategory_id=55,
                offers={77: object()},
            )
        ),
    )

    assert await FunPayChatGateway(bot).delete_offer(
        77,
        expected_provenance_token="0123456789abcdef0123456789abcdef",
    ) is False
    assert bot.get_my_offers_page.await_count == 2


async def test_engine_07_delete_rejects_empty_wrong_page_as_postcondition(
    monkeypatch,
):
    import app.integrations.funpay.gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "_DELETE_OFFER_VERIFICATION_DELAYS",
        (0.0, 0.0),
    )
    fields = _engine_offer_fields()
    fields.offer_id = 77
    fields.subcategory_id = 55
    fields.desc_ru = "[FPBOT:0123456789abcdef0123456789abcdef]"
    fields.desc_en = "[FPBOT:0123456789abcdef0123456789abcdef]"
    bot = SimpleNamespace(
        get_offer_fields=AsyncMock(return_value=fields),
        save_offer_fields=AsyncMock(return_value=True),
        get_my_offers_page=AsyncMock(
            return_value=SimpleNamespace(subcategory_id=0, offers={})
        ),
    )

    assert await FunPayChatGateway(bot).delete_offer(
        77,
        expected_provenance_token="0123456789abcdef0123456789abcdef",
    ) is False
    assert bot.get_my_offers_page.await_count == 2


async def test_real_gateway_rejects_missing_message_acknowledgement():
    bot = SimpleNamespace(send_message=AsyncMock(return_value=None))

    with pytest.raises(FunPayApiError):
        await FunPayChatGateway(bot).send_message(123, "hello")


async def test_real_gateway_reads_full_offer_descriptions_for_provenance():
    bot = SimpleNamespace(get_offer_fields=AsyncMock(return_value=SimpleNamespace(
        desc_ru="Описание\n\n[FPBOT:0123456789abcdef0123456789abcdef]",
        desc_en="   ",
    )))

    descriptions = await FunPayChatGateway(bot).get_offer_descriptions(77)

    assert descriptions == (
        "Описание\n\n[FPBOT:0123456789abcdef0123456789abcdef]",
        None,
    )
    bot.get_offer_fields.assert_awaited_once_with(offer_id=77)


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


def test_build_order_info_preserves_full_description_for_provenance():
    from funpayparsers.types.enums import OrderStatus as FPOrderStatus

    buyer = SimpleNamespace(
        id=5,
        username="buyer",
        avatar_url=None,
        online=True,
        status_text="online",
    )
    page = SimpleNamespace(
        order_id="ORDER42",
        order_status=FPOrderStatus.PAID,
        chat=SimpleNamespace(id=10, interlocutor=buyer),
        order_total=SimpleNamespace(value=100),
        order_subcategory_id=55,
        short_description="Display title",
        full_description="Details\n\n[FPBOT:0123456789abcdef0123456789abcdef]",
        data={},
    )

    info = _build_order_info(page)

    assert info.full_description == page.full_description
    assert info.offer_id is None
