from datetime import datetime, timezone

from app.integrations.funpay.types import (
    OrderInfo,
    OfferInfo,
    MessageInfo,
    SaleStatus,
    OfferFieldsDTO,
)


def test_order_info_minimal():
    order = OrderInfo(
        order_id="123456",
        status=SaleStatus.PAID,
        chat_id=789,
        buyer_id=111,
        subcategory_id=55,
        title="ChatGPT Plus 7 days",
        price=599.0,
    )
    assert order.order_id == "123456"
    assert order.status is SaleStatus.PAID
    assert order.chat_id == 789


def test_offer_info():
    offer = OfferInfo(
        offer_id=100,
        subcategory_id=55,
        title="Test",
        price=500.0,
        active=True,
        auto_delivery=False,
    )
    assert offer.offer_id == 100
    assert offer.active is True


def test_message_info():
    msg = MessageInfo(
        message_id=1,
        chat_id=100,
        sender_id=200,
        text="!код",
        order_id="123456",
    )
    assert msg.text == "!код"
    assert msg.order_id == "123456"


def test_offer_fields_dto_build():
    fields = OfferFieldsDTO(
        offer_id=0,
        subcategory_id=55,
        title_ru="Тест",
        title_en="Test",
        desc_ru="Описание",
        desc_en="Desc",
        price=500.0,
        active=True,
        auto_delivery=False,
    )
    assert fields.offer_id == 0
    assert fields.title_ru == "Тест"


def test_sale_status_values():
    assert SaleStatus.PAID != SaleStatus.COMPLETED
    assert SaleStatus.COMPLETED != SaleStatus.REFUNDED
