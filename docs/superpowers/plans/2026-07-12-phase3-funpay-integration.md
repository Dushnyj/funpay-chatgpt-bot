# Фаза 3: Интеграция FunPay — План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Построить слой интеграции с FunPay (ChatGateway + обработка событий + sync лотов + bump), изолированный за Protocol, тестируемый без реального FunPay.

**Architecture:** funpaybotengine (Bot + Dispatcher + WebSocket events) обёрнут в FunPayChatGateway, реализующий ChatGateway Protocol. Все сервисы (OrderProcessor, LotSyncService, BumpService, CommandRouter) зависят от ChatGateway, в тестах подставляется FakeChatGateway. События FunPay (NewSaleEvent, SaleClosedEvent, SaleRefundedEvent, NewMessageEvent) адаптируются в наши доменные callback'и через FunPayRunner.

**Tech Stack:** Python 3.12, funpaybotengine==0.7.0 (+ funpayparsers==0.10.0 как транзитивная), SQLAlchemy async, pytest-asyncio.

---

## API-реалии funpaybotengine (критичные отличия от спеки)

Зафиксировано по исходникам funpaybotengine@dev (v0.7.0 на PyPI):

1. **Нет `complete_order()`.** Подтверждение сделки — действие покупателя. Продавец доставляет данные через чат (`send_message`). Статус приходит как `SaleClosedEvent` (покупатель подтвердил) или `SaleRefundedEvent` (возврат).
2. **Статусы FunPay** (`OrderStatus` enum): `PAID` (оплачен, ждёт подтверждения покупателем), `COMPLETED` (завершён), `REFUNDED` (возврат). Нет отдельного `dispute`/`cancelled`.
3. **`raise_offers(category_id, *subcategory_ids)`** — бампит **категорию/подкатегорию** целиком, не отдельный лот. Возвращает `RaiseOffersResponse`.
4. **Пауза/активация отдельного лота** — через `save_offer_fields(OfferFields)` с `active=True/False`. NOT `set_offers_hidden` (это глобальный переключатель ВСЕХ офферов — только для emergency).
5. **Создание нового лота** — `save_offer_fields` с `offer_id=0` + `subcategory_id`.
6. **Message API:** `message.reply(text)` или `bot.send_message(chat_id, text)`. `message.meta.order_id` — связка сообщения с заказом.
7. **События:** `NewSaleEvent` (новый заказ), `SaleClosedEvent` (подтверждён покупателем), `SaleRefundedEvent` (возврат), `NewMessageEvent` (сообщение в чате). Регистрация: `@dp.on_new_sale()`, `@dp.on_sale_closed()`, `@dp.on_sale_refunded()`, `@dp.on_new_message()`.
8. **Bot lifecycle:** `bot = Bot(golden_key)`, `await bot.update()` (инициализация), `await bot.listen_events(dp)` (WebSocket loop), `await bot.stop_listening()`.
9. **OrderPage** содержит: `order_id`, `order_status`, `order_subcategory_id`, `short_description`, `full_description`, `order_total`, `chat` (объект Chat с id).

---

## Структура файлов

### Новые файлы

```
backend/app/integrations/funpay/
├── __init__.py          # пустой
├── exceptions.py        # FunPayError, GoldenKeyError, FunPayApiError
├── types.py             # DTO: OrderInfo, OfferInfo, MessageInfo, FunPayEvent
├── gateway.py           # ChatGateway (Protocol) + FunPayChatGateway (impl)
└── runner.py            # FunPayRunner — Bot+Dispatcher lifecycle + handler registry

backend/app/services/
├── command_parser.py    # CommandType enum + ParsedCommand + CommandParser
├── command_router.py    # CommandRouter — callback-диспетчер команд
├── order_processor.py   # OrderProcessor — Order CRUD, идемпотентность, lot matching
├── lot_sync.py          # LotSyncService — sync DB lots ↔ FunPay offers
└── bump.py              # BumpService — bump категории + запись BumpLog

backend/tests/
├── test_funpay_exceptions.py
├── test_funpay_types.py
├── test_funpay_gateway.py        # FakeChatGateway + FunPayChatGateway mapping tests
├── test_command_parser.py
├── test_command_router.py
├── test_order_processor.py
├── test_lot_sync.py
├── test_bump.py
└── test_funpay_runner.py
```

### Модифицируемые файлы

- `backend/pyproject.toml` — добавить `funpaybotengine>=0.7.0` в dependencies

---

## Task 1: Добавить зависимость funpaybotengine

**Files:**
- Modify: `backend/pyproject.toml`
- Create: `backend/app/integrations/funpay/__init__.py`

- [ ] **Step 1: Добавить funpaybotengine в dependencies**

В `backend/pyproject.toml`, в список `dependencies` добавить строку (после `python-telegram-bot`):

```toml
    "funpaybotengine>=0.7.0",
```

- [ ] **Step 2: Создать пакет integrations/funpay**

Создать `backend/app/integrations/funpay/__init__.py` (пустой файл):

```python
```

- [ ] **Step 3: Проверить, что pyproject валиден**

Run: `cd /c/Source/funpay/backend && py -3.12 -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))" && echo OK`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay/backend
git add pyproject.toml app/integrations/funpay/__init__.py
git commit -m "chore: add funpaybotengine dependency"
```

---

## Task 2: FunPay исключения

**Files:**
- Create: `backend/app/integrations/funpay/exceptions.py`
- Test: `backend/tests/test_funpay_exceptions.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_funpay_exceptions.py`:

```python
import pytest

from app.integrations.funpay.exceptions import (
    FunPayError,
    FunPayApiError,
    GoldenKeyError,
)


def test_funpay_error_is_exception():
    assert issubclass(FunPayError, Exception)


def test_funpay_api_error_carries_status_and_body():
    err = FunPayApiError(status=403, body="forbidden")
    assert err.status == 403
    assert err.body == "forbidden"
    assert isinstance(err, FunPayError)
    assert "403" in str(err)


def test_golden_key_error_is_funpay_error():
    err = GoldenKeyError("session expired")
    assert isinstance(err, FunPayError)
    assert "session expired" in str(err)
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_exceptions.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/integrations/funpay/exceptions.py`:

```python
class FunPayError(Exception):
    """Базовое исключение для всех ошибок FunPay-слоя."""


class GoldenKeyError(FunPayError):
    """golden_key протух или невалиден — требуется перевыпуск."""


class FunPayApiError(FunPayError):
    """HTTP-ошибка вызова FunPay API с сохранённым ответом."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"FunPay API error {status}: {body}")
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_exceptions.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/integrations/funpay/exceptions.py tests/test_funpay_exceptions.py
git commit -m "feat: add FunPay integration exceptions"
```

---

## Task 3: DTO типы для FunPay-слоя

Изолируем домен от типов funpaybotengine: наши DTO, в которые Gateway маппит внешние объекты.

**Files:**
- Create: `backend/app/integrations/funpay/types.py`
- Test: `backend/tests/test_funpay_types.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_funpay_types.py`:

```python
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
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_types.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/integrations/funpay/types.py`:

```python
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
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_types.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/integrations/funpay/types.py tests/test_funpay_types.py
git commit -m "feat: add FunPay DTO types isolating domain from library"
```

---

## Task 4: ChatGateway Protocol + FakeChatGateway (test double)

Protocol-интерфейс изоляции FunPay-слоя. Все сервисы зависят от ChatGateway, в тестах — FakeChatGateway. Это позволяет тестировать всю бизнес-логику без реального FunPay.

**Files:**
- Create: `backend/app/integrations/funpay/gateway.py` (Protocol часть)
- Test: `backend/tests/test_funpay_gateway.py` (FakeChatGateway поведение)

- [ ] **Step 1: Написать тест для FakeChatGateway**

`backend/tests/test_funpay_gateway.py`:

```python
import pytest

from app.integrations.funpay.gateway import FakeChatGateway
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
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_gateway.py -v`
Expected: FAIL (ModuleNotFoundError / ImportError)

- [ ] **Step 3: Реализовать Protocol + FakeChatGateway**

`backend/app/integrations/funpay/gateway.py`:

```python
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
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_gateway.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/integrations/funpay/gateway.py tests/test_funpay_gateway.py
git commit -m "feat: add ChatGateway Protocol and FakeChatGateway test double"
```

---

## Task 5: FunPayChatGateway — реализация над funpaybotengine.Bot

Тонкая обёртка над Bot. Маппит типы funpaybotengine → наши DTO. НЕ тестируется интеграционно (нет реального golden_key) — только проверка маппинга через подмену Bot.

**Files:**
- Modify: `backend/app/integrations/funpay/gateway.py` (добавить FunPayChatGateway)
- Test: `backend/tests/test_funpay_gateway.py` (добавить маппинг-тесты)

- [ ] **Step 1: Дописать тест для FunPayChatGateway mapping функций**

Добавить в конец `backend/tests/test_funpay_gateway.py`:

```python
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
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_gateway.py::test_map_order_status_paid -v`
Expected: FAIL (ImportError: cannot import name '_map_order_status')

- [ ] **Step 3: Реализовать mapping функции и FunPayChatGateway класс**

Добавить в конец `backend/app/integrations/funpay/gateway.py`:

```python
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
        from funpaybotengine.types import OfferFields
        fp_fields = await self._bot.get_offer_fields(
            subcategory_id=fields.subcategory_id,
            offer_id=fields.offer_id if fields.offer_id > 0 else None,
        ) if fields.offer_id > 0 else await self._bot.get_offer_fields(
            subcategory_id=fields.subcategory_id,
        )
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
        from funpaybotengine.types import OfferFields
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
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_gateway.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/integrations/funpay/gateway.py tests/test_funpay_gateway.py
git commit -m "feat: add FunPayChatGateway wrapping funpaybotengine.Bot"
```

---

## Task 6: CommandParser — парсинг команд !код/!code

Парсит текст сообщения, определяет команду по алиасу. Case-insensitive, префикс `!`. Без бизнес-логики — только идентификация команды и extract аргументов.

**Files:**
- Create: `backend/app/services/command_parser.py`
- Test: `backend/tests/test_command_parser.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_command_parser.py`:

```python
import pytest

from app.services.command_parser import (
    CommandParser,
    CommandType,
    ParsedCommand,
)


@pytest.fixture
def parser() -> CommandParser:
    return CommandParser()


def test_parse_code_ru(parser: CommandParser):
    result = parser.parse("!код")
    assert result is not None
    assert result.command is CommandType.CODE
    assert result.argument is None


def test_parse_code_en(parser: CommandParser):
    result = parser.parse("!code")
    assert result is not None
    assert result.command is CommandType.CODE


def test_parse_case_insensitive(parser: CommandParser):
    assert parser.parse("!КОД").command is CommandType.CODE
    assert parser.parse("!Code").command is CommandType.CODE
    assert parser.parse("!SUB").command is CommandType.SUBSCRIPTION


def test_parse_subscription_ru(parser: CommandParser):
    assert parser.parse("!подписка").command is CommandType.SUBSCRIPTION


def test_parse_subscription_en(parser: CommandParser):
    assert parser.parse("!sub").command is CommandType.SUBSCRIPTION


def test_parse_replace_ru(parser: CommandParser):
    assert parser.parse("!замена").command is CommandType.REPLACE


def test_parse_replace_en(parser: CommandParser):
    assert parser.parse("!replace").command is CommandType.REPLACE


def test_parse_seller_ru(parser: CommandParser):
    assert parser.parse("!продавец").command is CommandType.SELLER


def test_parse_seller_en(parser: CommandParser):
    assert parser.parse("!seller").command is CommandType.SELLER


def test_parse_help_ru(parser: CommandParser):
    assert parser.parse("!помощь").command is CommandType.HELP


def test_parse_help_en(parser: CommandParser):
    assert parser.parse("!help").command is CommandType.HELP


def test_parse_with_argument(parser: CommandParser):
    result = parser.parse("!код что-то лишнее")
    assert result is not None
    assert result.command is CommandType.CODE
    assert result.argument == "что-то лишнее"


def test_parse_non_command_returns_none(parser: CommandParser):
    assert parser.parse("привет") is None
    assert parser.parse("hello world") is None
    assert parser.parse("") is None


def test_parse_exclamation_no_match(parser: CommandParser):
    assert parser.parse("!неизвестная") is None


def test_parse_ignores_leading_whitespace(parser: CommandParser):
    result = parser.parse("  !код")
    assert result is not None
    assert result.command is CommandType.CODE


def test_parsed_command_is_frozen(parser: CommandParser):
    result = parser.parse("!код")
    with pytest.raises(Exception):
        result.command = CommandType.HELP  # type: ignore
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_parser.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/command_parser.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class CommandType(Enum):
    """Типы команд, поддерживаемых ботом в чатах сделок FunPay."""

    CODE = auto()
    SUBSCRIPTION = auto()
    REPLACE = auto()
    SELLER = auto()
    HELP = auto()


@dataclass(frozen=True)
class ParsedCommand:
    """Результат парсинга: определённая команда + хвост текста как аргумент."""

    command: CommandType
    argument: str | None


# Алиасы RU/EN для каждой команды. Match по lowercased префиксу без `!`.
_ALIASES: dict[str, CommandType] = {
    "код": CommandType.CODE,
    "code": CommandType.CODE,
    "подписка": CommandType.SUBSCRIPTION,
    "sub": CommandType.SUBSCRIPTION,
    "замена": CommandType.REPLACE,
    "replace": CommandType.REPLACE,
    "продавец": CommandType.SELLER,
    "seller": CommandType.SELLER,
    "помощь": CommandType.HELP,
    "help": CommandType.HELP,
}


class CommandParser:
    """Парсер команд из текста сообщений чата FunPay.

    Команда — префикс `!` + алиас (case-insensitive). Остаток строки — аргумент.
    """

    def parse(self, text: str | None) -> ParsedCommand | None:
        if not text:
            return None
        stripped = text.strip()
        if not stripped.startswith("!"):
            return None
        body = stripped[1:].strip()
        if not body:
            return None
        parts = body.split(maxsplit=1)
        alias = parts[0].lower()
        cmd = _ALIASES.get(alias)
        if cmd is None:
            return None
        argument = parts[1].strip() if len(parts) > 1 else None
        return ParsedCommand(command=cmd, argument=argument)
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_parser.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/services/command_parser.py tests/test_command_parser.py
git commit -m "feat: add CommandParser for !code/!sub/!replace/!seller/!help"
```

---

## Task 7: CommandRouter — callback-диспетчер команд

Регистрирует хэндлеры по CommandType. При получении сообщения: парсит → маршрутизирует в хэндлер. Хэндлеры — async callable'ы, реализуемые в Фазе 4 (бизнес-логика).

**Files:**
- Create: `backend/app/services/command_router.py`
- Test: `backend/tests/test_command_router.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_command_router.py`:

```python
from unittest.mock import AsyncMock

import pytest

from app.integrations.funpay.gateway import FakeChatGateway
from app.services.command_parser import CommandType
from app.services.command_router import (
    CommandRouter,
    CommandContext,
    UnhandledMessage,
)


@pytest.fixture
def router() -> CommandRouter:
    return CommandRouter()


@pytest.fixture
def gateway() -> FakeChatGateway:
    return FakeChatGateway()


def _ctx(router: CommandRouter, gateway: FakeChatGateway, text: str, chat_id: int = 100,
         order_id: str | None = "500", lang: str = "ru") -> CommandContext:
    return router.build_context(
        chat_id=chat_id,
        sender_id=200,
        text=text,
        order_id=order_id,
        lang=lang,
        gateway=gateway,
    )


async def test_route_code_calls_registered_handler(router: CommandRouter, gateway: FakeChatGateway):
    handler = AsyncMock()
    router.register(CommandType.CODE, handler)
    ctx = _ctx(router, gateway, "!код")
    await router.dispatch(ctx)
    handler.assert_awaited_once_with(ctx)


async def test_route_unknown_command_does_nothing(router: CommandRouter, gateway: FakeChatGateway):
    handler = AsyncMock()
    router.register(CommandType.CODE, handler)
    ctx = _ctx(router, gateway, "привет")
    await router.dispatch(ctx)
    handler.assert_not_awaited()


async def test_unregistered_command_raises_unhandled(router: CommandRouter, gateway: FakeChatGateway):
    ctx = _ctx(router, gateway, "!помощь")
    with pytest.raises(UnhandledMessage):
        await router.dispatch(ctx)


async def test_register_overwrites_previous(router: CommandRouter, gateway: FakeChatGateway):
    first = AsyncMock()
    second = AsyncMock()
    router.register(CommandType.CODE, first)
    router.register(CommandType.CODE, second)
    ctx = _ctx(router, gateway, "!код")
    await router.dispatch(ctx)
    first.assert_not_awaited()
    second.assert_awaited_once_with(ctx)


def test_build_context_parses_command(router: CommandRouter, gateway: FakeChatGateway):
    ctx = _ctx(router, gateway, "!код")
    assert ctx.parsed is not None
    assert ctx.parsed.command is CommandType.CODE


def test_build_context_none_parsed_for_non_command(router: CommandRouter, gateway: FakeChatGateway):
    ctx = _ctx(router, gateway, "обычное сообщение")
    assert ctx.parsed is None
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_router.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/command_router.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from app.integrations.funpay.gateway import ChatGateway
from app.services.command_parser import CommandParser, ParsedCommand


@dataclass(frozen=True)
class CommandContext:
    """Контекст обработки сообщения из чата FunPay.

    Передаётся в хэндлер команды. Содержит всё для ответа и бизнес-логики.
    """

    chat_id: int
    sender_id: int
    text: str
    order_id: str | None
    lang: str
    gateway: ChatGateway
    parsed: ParsedCommand | None


CommandHandler = Callable[[CommandContext], Awaitable[None]]


class UnhandledMessage(Exception):
    """Команда распознана, но для неё нет зарегистрированного хэндлера."""


class CommandRouter:
    """Диспетчер команд: парсит → маршрутизирует в зарегистрированный хэндлер.

    Хэндлеры регистрируются по CommandType (Фаза 4 подключит реальные сервисы).
    Нераспознанные сообщения игнорируются (return None).
    Распознанная команда без хэндлера → UnhandledMessage.
    """

    def __init__(self, parser: CommandParser | None = None) -> None:
        self._parser = parser or CommandParser()
        self._handlers: dict = {}

    def register(self, command, handler: CommandHandler) -> None:
        self._handlers[command] = handler

    def build_context(
        self,
        chat_id: int,
        sender_id: int,
        text: str,
        order_id: str | None,
        lang: str,
        gateway: ChatGateway,
    ) -> CommandContext:
        parsed = self._parser.parse(text)
        return CommandContext(
            chat_id=chat_id,
            sender_id=sender_id,
            text=text,
            order_id=order_id,
            lang=lang,
            gateway=gateway,
            parsed=parsed,
        )

    async def dispatch(self, ctx: CommandContext) -> None:
        if ctx.parsed is None:
            return
        handler = self._handlers.get(ctx.parsed.command)
        if handler is None:
            raise UnhandledMessage(
                f"No handler registered for {ctx.parsed.command}"
            )
        await handler(ctx)
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_router.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/services/command_router.py tests/test_command_router.py
git commit -m "feat: add CommandRouter with callback-based command dispatch"
```

---

## Task 8: OrderProcessor — идемпотентное создание/обновление Order

Обработка нового заказа: создание Order в БД (идемпотентно по funpay_order_id), определение Lot по subcategory. НЕ выдаёт аккаунт (Фаза 4).

**Files:**
- Create: `backend/app/services/order_processor.py`
- Test: `backend/tests/test_order_processor.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_order_processor.py`:

```python
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import OrderInfo, SaleStatus
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot
from app.models.rental import Order
from app.services.order_processor import OrderProcessor, LotNotFoundError


@pytest.fixture
def gateway() -> FakeChatGateway:
    gw = FakeChatGateway()
    gw.set_order(OrderInfo(
        order_id="ord-1",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="ChatGPT Plus 7d",
        price=599.0,
    ))
    return gw


async def _seed_catalog_and_lot(session: AsyncSession, funpay_node_id: int = 55) -> int:
    """Создаёт tier+duration+scope+lot и возвращает lot_id."""
    tier = SubscriptionTier(name="Plus", chatgpt_plan="plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(name="any", min_limit_pct=None, is_enabled=True)
    session.add(scope)
    await session.flush()
    lot = Lot(
        funpay_node_id=funpay_node_id,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Plus 7d",
        title_en="Plus 7d",
        status="active",
        auto_created=True,
    )
    session.add(lot)
    await session.flush()
    return lot.id


async def test_process_new_sale_creates_order(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    order = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert order.funpay_order_id == "ord-1"
    assert order.funpay_chat_id == "100"
    assert order.buyer_funpay_id == "200"
    assert order.lot_id is not None
    assert order.status == "pending"


async def test_process_new_sale_idempotent(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    first = await proc.process_new_sale(session, gateway, order_id="ord-1")
    second = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert first.id == second.id
    result = await session.execute(select(Order).where(Order.funpay_order_id == "ord-1"))
    assert len(result.scalars().all()) == 1


async def test_process_new_sale_no_matching_lot_raises(
    session: AsyncSession, gateway: FakeChatGateway,
):
    proc = OrderProcessor()
    with pytest.raises(LotNotFoundError):
        await proc.process_new_sale(session, gateway, order_id="ord-1")


async def test_process_sale_closed_marks_completed(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    await proc.process_new_sale(session, gateway, order_id="ord-1")
    order = await proc.process_sale_closed(session, order_id="ord-1")
    assert order.status == "completed"


async def test_process_sale_refunded_marks_refunded(session: AsyncSession, gateway: FakeChatGateway):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    await proc.process_new_sale(session, gateway, order_id="ord-1")
    order = await proc.process_sale_refunded(session, order_id="ord-1")
    assert order.status == "refunded"


async def test_process_sale_closed_unknown_order_raises(session: AsyncSession):
    proc = OrderProcessor()
    with pytest.raises(KeyError):
        await proc.process_sale_closed(session, order_id="nope")


async def test_process_new_sale_records_tier_duration_scope(
    session: AsyncSession, gateway: FakeChatGateway,
):
    await _seed_catalog_and_lot(session)
    proc = OrderProcessor()
    order = await proc.process_new_sale(session, gateway, order_id="ord-1")
    assert order.tier_id is not None
    assert order.duration_id is not None
    assert order.limit_scope_id is not None
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_order_processor.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/order_processor.py`:

```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.lot import Lot
from app.models.rental import Order


class LotNotFoundError(Exception):
    """Для заказа не найден Lot с matching funpay_node_id."""


class OrderProcessor:
    """Обработка событий заказа: создание, обновление статуса.

    Создание идемпотентно по funpay_order_id. Определяет lot по funpay_node_id.
    НЕ выдаёт аккаунт — это ответственность Фазы 4 (AccountPool).
    """

    async def process_new_sale(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: str,
    ) -> Order:
        existing = await self._find_order(session, order_id)
        if existing is not None:
            return existing

        info = await gateway.get_order(order_id)
        lot = await self._find_lot_by_node(session, info.subcategory_id)
        if lot is None:
            raise LotNotFoundError(
                f"No active Lot for funpay_node_id={info.subcategory_id} "
                f"(order {order_id})"
            )
        order = Order(
            funpay_order_id=info.order_id,
            funpay_chat_id=str(info.chat_id),
            buyer_funpay_id=str(info.buyer_id),
            buyer_locale="ru",
            lot_id=lot.id,
            tier_id=lot.tier_id,
            duration_id=lot.duration_id,
            limit_scope_id=lot.limit_scope_id,
            min_limit_pct=lot.min_limit_pct,
            max_5h_pct=lot.max_5h_pct,
            max_weekly_pct=lot.max_weekly_pct,
            price=lot.price,
            status="pending",
        )
        session.add(order)
        await session.flush()
        return order

    async def process_sale_closed(
        self,
        session: AsyncSession,
        order_id: str,
    ) -> Order:
        order = await self._get_order_or_raise(session, order_id)
        order.status = "completed"
        await session.flush()
        return order

    async def process_sale_refunded(
        self,
        session: AsyncSession,
        order_id: str,
    ) -> Order:
        order = await self._get_order_or_raise(session, order_id)
        order.status = "refunded"
        await session.flush()
        return order

    async def _find_order(self, session: AsyncSession, order_id: str) -> Order | None:
        result = await session.execute(
            select(Order).where(Order.funpay_order_id == order_id)
        )
        return result.scalar_one_or_none()

    async def _get_order_or_raise(self, session: AsyncSession, order_id: str) -> Order:
        order = await self._find_order(session, order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found")
        return order

    async def _find_lot_by_node(
        self,
        session: AsyncSession,
        funpay_node_id: int,
    ) -> Lot | None:
        result = await session.execute(
            select(Lot).where(
                Lot.funpay_node_id == funpay_node_id,
                Lot.status == "active",
            )
        )
        return result.scalar_one_or_none()
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_order_processor.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/services/order_processor.py tests/test_order_processor.py
git commit -m "feat: add OrderProcessor with idempotent Order creation"
```

---

## Task 9: LotSyncService — генерация OfferFieldsDTO из Lot

Конвертация доменного Lot → OfferFieldsDTO для gateway.save_offer_fields(). Использует LotTemplate для title/description (если задан) или поля самого Lot.

**Files:**
- Create: `backend/app/services/lot_sync.py`
- Test: `backend/tests/test_lot_sync.py`

- [ ] **Step 1: Написать тест (часть 1: build_offer_fields)**

`backend/tests/test_lot_sync.py`:

```python
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot
from app.services.lot_sync import build_offer_fields


async def _make_lot(session: AsyncSession) -> Lot:
    tier = SubscriptionTier(name="Plus", chatgpt_plan="plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(name="any", min_limit_pct=None, is_enabled=True)
    session.add(scope)
    await session.flush()
    lot = Lot(
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Plus 7 дней",
        title_en="Plus 7 days",
        description_ru="Описание лота",
        description_en="Lot description",
        status="active",
        auto_created=True,
    )
    session.add(lot)
    await session.flush()
    return lot


async def test_build_offer_fields_new_lot(session: AsyncSession):
    lot = await _make_lot(session)
    fields = build_offer_fields(lot, offer_id=0, active=True)
    assert fields.offer_id == 0
    assert fields.subcategory_id == 55
    assert fields.title_ru == "Plus 7 дней"
    assert fields.title_en == "Plus 7 days"
    assert fields.desc_ru == "Описание лота"
    assert fields.desc_en == "Lot description"
    assert fields.price == 599.0
    assert fields.active is True
    assert fields.auto_delivery is False


async def test_build_offer_fields_existing_lot(session: AsyncSession):
    lot = await _make_lot(session)
    fields = build_offer_fields(lot, offer_id=42, active=False)
    assert fields.offer_id == 42
    assert fields.active is False


async def test_build_offer_fields_uses_node_id_as_subcategory(session: AsyncSession):
    lot = await _make_lot(session)
    fields = build_offer_fields(lot, offer_id=0, active=True)
    assert fields.subcategory_id == lot.funpay_node_id
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_lot_sync.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать build_offer_fields (первая часть файла)**

`backend/app/services/lot_sync.py`:

```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.integrations.funpay.types import OfferFieldsDTO
from app.models.lot import Lot


def build_offer_fields(lot: Lot, offer_id: int, active: bool) -> OfferFieldsDTO:
    """Сборка OfferFieldsDTO из доменного Lot.

    offer_id=0 — создание нового лота на FunPay.
    subcategory_id = lot.funpay_node_id (ID ноды FunPay, куда публикуется лот).
    """
    return OfferFieldsDTO(
        offer_id=offer_id,
        subcategory_id=lot.funpay_node_id or 0,
        title_ru=lot.title_ru,
        title_en=lot.title_en,
        desc_ru=lot.description_ru or "",
        desc_en=lot.description_en or "",
        price=float(lot.price),
        active=active,
        auto_delivery=False,
    )
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_lot_sync.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/services/lot_sync.py tests/test_lot_sync.py
git commit -m "feat: add build_offer_fields for Lot-to-OfferFieldsDTO conversion"
```

---

## Task 10: LotSyncService — sync операций (create/pause/activate)

Sync одного лота с FunPay: если funpay_id пуст — создаёт; если есть — обновляет/паузит/активирует. Обновляет lot.funpay_id после создания.

**Files:**
- Modify: `backend/app/services/lot_sync.py` (добавить LotSyncService)
- Modify: `backend/tests/test_lot_sync.py` (добавить тесты sync)

- [ ] **Step 1: Дописать тесты для LotSyncService**

Добавить в конец `backend/tests/test_lot_sync.py`:

```python
from app.integrations.funpay.gateway import FakeChatGateway
from app.services.lot_sync import LotSyncService
from sqlalchemy import select as sa_select


@pytest.fixture
def gateway() -> FakeChatGateway:
    return FakeChatGateway()


async def test_sync_creates_new_offer(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = None
    await session.flush()
    svc = LotSyncService()
    funpay_id = await svc.sync_lot(session, gateway, lot.id, active=True)
    assert funpay_id  # вернул новый ID
    lot_id_check = funpay_id
    await session.refresh(lot)
    assert lot.funpay_id == str(funpay_id)
    assert lot_id_check in gateway.saved_offers
    saved = gateway.saved_offers[lot_id_check]
    assert saved.active is True
    assert saved.title_ru == "Plus 7 дней"


async def test_sync_updates_existing_offer(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = "100"
    await session.flush()
    svc = LotSyncService()
    funpay_id = await svc.sync_lot(session, gateway, lot.id, active=False)
    assert funpay_id == 100
    assert gateway.saved_offers[100].active is False


async def test_sync_pause_uses_set_offer_active(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = "200"
    await session.flush()
    svc = LotSyncService()
    await svc.pause_lot(session, gateway, lot.id)
    assert (200, False) in gateway.activity_changes


async def test_sync_activate_uses_set_offer_active(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    lot.funpay_id = "300"
    await session.flush()
    svc = LotSyncService()
    await svc.activate_lot(session, gateway, lot.id)
    assert (300, True) in gateway.activity_changes


async def test_pause_lot_without_funpay_id_raises(session: AsyncSession, gateway: FakeChatGateway):
    from app.services.lot_sync import LotNotPublishedError
    lot = await _make_lot(session)
    lot.funpay_id = None
    await session.flush()
    svc = LotSyncService()
    with pytest.raises(LotNotPublishedError):
        await svc.pause_lot(session, gateway, lot.id)
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_lot_sync.py::test_sync_creates_new_offer -v`
Expected: FAIL (ImportError: LotSyncService)

- [ ] **Step 3: Реализовать LotSyncService**

Добавить в конец `backend/app/services/lot_sync.py`:

```python
class LotNotPublishedError(Exception):
    """Лот ещё не опубликован на FunPay (funpay_id is None)."""


class LotNotFoundError(Exception):
    """Lot с указанным ID не найден в БД."""


class LotSyncService:
    """Синхронизация состояния лота между БД и FunPay.

    sync_lot: создаёт новый (funpay_id is None) или обновляет существующий.
    pause_lot/activate_lot: переключение active без перезаписи остальных полей.
    """

    async def sync_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot_id: int,
        active: bool,
    ) -> int:
        lot = await self._get_lot(session, lot_id)
        if lot.funpay_id:
            offer_id = int(lot.funpay_id)
        else:
            offer_id = 0
        fields = build_offer_fields(lot, offer_id=offer_id, active=active)
        result_id = await gateway.save_offer_fields(fields)
        if not lot.funpay_id:
            lot.funpay_id = str(result_id)
            await session.flush()
        return result_id

    async def pause_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot_id: int,
    ) -> None:
        lot = await self._get_lot(session, lot_id)
        if not lot.funpay_id:
            raise LotNotPublishedError(f"Lot {lot_id} has no funpay_id")
        await gateway.set_offer_active(int(lot.funpay_id), active=False)
        lot.status = "paused"
        await session.flush()

    async def activate_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot_id: int,
    ) -> None:
        lot = await self._get_lot(session, lot_id)
        if not lot.funpay_id:
            raise LotNotPublishedError(f"Lot {lot_id} has no funpay_id")
        await gateway.set_offer_active(int(lot.funpay_id), active=True)
        lot.status = "active"
        await session.flush()

    async def _get_lot(self, session: AsyncSession, lot_id: int) -> Lot:
        lot = await session.get(Lot, lot_id)
        if lot is None:
            raise LotNotFoundError(f"Lot {lot_id} not found")
        return lot
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_lot_sync.py -v`
Expected: PASS (8 tests: 3 из Task 9 + 5 новых)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/services/lot_sync.py tests/test_lot_sync.py
git commit -m "feat: add LotSyncService for create/pause/activate lot on FunPay"
```

---

## Task 11: BumpService — bump категории + запись BumpLog

Вызов gateway.bump_category() и запись результата в BumpLog. Равномерное распределение: один bump за вызов.

**Files:**
- Create: `backend/app/services/bump.py`
- Test: `backend/tests/test_bump.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_bump.py`:

```python
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot, BumpLog
from app.services.bump import BumpService, BumpResult


@pytest.fixture
def gateway() -> FakeChatGateway:
    return FakeChatGateway()


async def _make_lot(session: AsyncSession) -> Lot:
    tier = SubscriptionTier(name="Plus", chatgpt_plan="plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(name="any", min_limit_pct=None, is_enabled=True)
    session.add(scope)
    await session.flush()
    lot = Lot(
        funpay_id="500",
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="T",
        title_en="T",
        status="active",
        auto_created=True,
    )
    session.add(lot)
    await session.flush()
    return lot


async def test_bump_lot_success(session: AsyncSession, gateway: FakeChatGateway):
    lot = await _make_lot(session)
    svc = BumpService()
    result = await svc.bump_lot(
        session, gateway, lot_id=lot.id, category_id=1, subcategory_id=55,
    )
    assert result.success is True
    assert (1, 55) in gateway.bumped
    # Записан BumpLog
    logs = (await session.execute(select(BumpLog).where(BumpLog.lot_id == lot.id))).scalars().all()
    assert len(logs) == 1
    assert logs[0].success is True
    assert logs[0].error is None


async def test_bump_lot_records_failure(session: AsyncSession):
    from app.integrations.funpay.gateway import FakeChatGateway

    class FailingGateway(FakeChatGateway):
        async def bump_category(self, category_id: int, subcategory_id: int) -> bool:
            raise RuntimeError("network error")

    lot = await _make_lot(session)
    svc = BumpService()
    result = await svc.bump_lot(
        session, FailingGateway(), lot_id=lot.id, category_id=1, subcategory_id=55,
    )
    assert result.success is False
    assert "network error" in (result.error or "")
    logs = (await session.execute(select(BumpLog).where(BumpLog.lot_id == lot.id))).scalars().all()
    assert len(logs) == 1
    assert logs[0].success is False
    assert "network error" in logs[0].error


async def test_needs_bump_no_history(session: AsyncSession):
    lot = await _make_lot(session)
    svc = BumpService()
    needs = await svc.needs_bump(session, lot.id, interval=timedelta(hours=4))
    assert needs is True


async def test_needs_bump_recent_history(session: AsyncSession):
    lot = await _make_lot(session)
    recent = BumpLog(
        lot_id=lot.id,
        bumped_at=datetime.now(timezone.utc) - timedelta(hours=1),
        success=True,
    )
    session.add(recent)
    await session.flush()
    svc = BumpService()
    needs = await svc.needs_bump(session, lot.id, interval=timedelta(hours=4))
    assert needs is False


async def test_needs_bump_old_history(session: AsyncSession):
    lot = await _make_lot(session)
    old = BumpLog(
        lot_id=lot.id,
        bumped_at=datetime.now(timezone.utc) - timedelta(hours=10),
        success=True,
    )
    session.add(old)
    await session.flush()
    svc = BumpService()
    needs = await svc.needs_bump(session, lot.id, interval=timedelta(hours=4))
    assert needs is True
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_bump.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/bump.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.lot import BumpLog, Lot


@dataclass(frozen=True)
class BumpResult:
    """Результат операции bump лота."""

    success: bool
    error: str | None = None


class BumpService:
    """Поднятие лотов на FunPay (bump категории) с записью в BumpLog.

    raise_offers бампит всю подкатегорию, но мы логируем per-lot,
    так как один лот = одна нода. Равномерность: один bump за вызов.
    """

    async def bump_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot_id: int,
        category_id: int,
        subcategory_id: int,
    ) -> BumpResult:
        lot = await session.get(Lot, lot_id)
        if lot is None:
            raise KeyError(f"Lot {lot_id} not found")
        try:
            await gateway.bump_category(category_id, subcategory_id)
        except Exception as exc:
            error = str(exc)
            await self._log(session, lot.id, success=False, error=error)
            return BumpResult(success=False, error=error)
        await self._log(session, lot.id, success=True, error=None)
        return BumpResult(success=True)

    async def needs_bump(
        self,
        session: AsyncSession,
        lot_id: int,
        interval: timedelta,
    ) -> bool:
        """Проверка: последний успешный bump старше interval (или его не было)."""
        last = await self._last_successful(session, lot_id)
        if last is None:
            return True
        return datetime.now(timezone.utc) - last.bumped_at >= interval

    async def _last_successful(self, session: AsyncSession, lot_id: int) -> BumpLog | None:
        result = await session.execute(
            select(BumpLog)
            .where(BumpLog.lot_id == lot_id, BumpLog.success.is_(True))
            .order_by(desc(BumpLog.bumped_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _log(
        self,
        session: AsyncSession,
        lot_id: int,
        success: bool,
        error: str | None,
    ) -> None:
        entry = BumpLog(
            lot_id=lot_id,
            bumped_at=datetime.now(timezone.utc),
            success=success,
            error=error,
        )
        session.add(entry)
        await session.flush()
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_bump.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/services/bump.py tests/test_bump.py
git commit -m "feat: add BumpService with BumpLog tracking"
```

---

## Task 12: FunPayRunner — lifecycle + handler registry

Оркестрация: создаёт Bot + Dispatcher, регистрирует callback'и для событий, управляет lifecycle (start/stop). Callback'и — async callable'ы, реализуемые в Фазе 4.

**Files:**
- Create: `backend/app/integrations/funpay/runner.py`
- Test: `backend/tests/test_funpay_runner.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_funpay_runner.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.funpay.runner import (
    FunPayRunner,
    SaleHandlers,
    MessageHandlers,
    RunnerCallbacks,
)


def test_runner_callbacks_defaults():
    callbacks = RunnerCallbacks()
    assert callbacks.on_new_sale is None
    assert callbacks.on_sale_closed is None
    assert callbacks.on_sale_refunded is None
    assert callbacks.on_message is None


def test_sale_handlers_stores_callbacks():
    new_sale = AsyncMock()
    closed = AsyncMock()
    refunded = AsyncMock()
    handlers = SaleHandlers(on_new_sale=new_sale, on_sale_closed=closed, on_sale_refunded=refunded)
    assert handlers.on_new_sale is new_sale


def test_message_handlers_stores_callback():
    on_msg = AsyncMock()
    handlers = MessageHandlers(on_message=on_msg)
    assert handlers.on_message is on_msg


def test_runner_callbacks_from_handlers():
    new_sale = AsyncMock()
    on_msg = AsyncMock()
    callbacks = RunnerCallbacks(
        on_new_sale=new_sale,
        on_message=on_msg,
    )
    assert callbacks.on_new_sale is new_sale
    assert callbacks.on_message is on_msg


def test_runner_stores_config():
    runner = FunPayRunner(
        golden_key="test-key",
        callbacks=RunnerCallbacks(),
        category_id=1,
    )
    assert runner.category_id == 1
    assert runner.callbacks.on_new_sale is None


def test_runner_callbacks_is_dataclass():
    from dataclasses import is_dataclass
    assert is_dataclass(RunnerCallbacks)
    assert is_dataclass(SaleHandlers)
    assert is_dataclass(MessageHandlers)
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_runner.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/integrations/funpay/runner.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from app.integrations.funpay.types import OrderInfo, MessageInfo


# Callback-типы: Фаза 4 подключит реальные реализации (OrderProcessor, CommandRouter)
NewSaleCallback = Callable[[str], Awaitable[None]]
SaleStatusCallback = Callable[[str], Awaitable[None]]
MessageCallback = Callable[[MessageInfo], Awaitable[None]]


@dataclass(frozen=True)
class SaleHandlers:
    """Хэндлеры событий заказа."""

    on_new_sale: NewSaleCallback | None = None
    on_sale_closed: SaleStatusCallback | None = None
    on_sale_refunded: SaleStatusCallback | None = None


@dataclass(frozen=True)
class MessageHandlers:
    """Хэндлер сообщений чата."""

    on_message: MessageCallback | None = None


@dataclass(frozen=True)
class RunnerCallbacks(SaleHandlers, MessageHandlers):
    """Все callback'и FunPay-событий в одном объекте.

    Атрибуты None — событие будет проигнорировано.
    """


class FunPayRunner:
    """Lifecycle-менеджер FunPay-соединения.

    Создаёт Bot + Dispatcher, регистрирует хэндлеры, управляет start/stop.
    Callback'и注入аются через RunnerCallbacks — Фаза 4 заполнит их реальными сервисами.
    """

    def __init__(
        self,
        golden_key: str,
        callbacks: RunnerCallbacks,
        category_id: int,
    ) -> None:
        self._golden_key = golden_key
        self.callbacks = callbacks
        self.category_id = category_id
        self._bot = None
        self._dp = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Инициализация Bot, регистрация хэндлеров, запуск WebSocket loop.

        В реальном приложении вызывается из lifecycle (FastAPI startup).
        """
        # Lazy import — не тянем funpaybotengine при импорте модуля
        from funpaybotengine import Bot, Dispatcher

        self._bot = Bot(golden_key=self._golden_key)
        self._dp = Dispatcher()
        self._register_handlers()
        await self._bot.update()
        self._started = True
        # listen_events запускается в отдельной задаче (background)
        import asyncio
        asyncio.create_task(self._bot.listen_events(self._dp))

    async def stop(self) -> None:
        """Остановка WebSocket loop."""
        if self._bot and self._started:
            await self._bot.stop_listening()
        self._started = False

    def _register_handlers(self) -> None:
        """Регистрация хэндлеров событий в Dispatcher."""
        if self._dp is None:
            return

        if self.callbacks.on_new_sale is not None:
            cb = self.callbacks.on_new_sale

            @self._dp.on_new_sale()
            async def handle_new_sale(event):
                order_id = event.object.meta.order_id
                await cb(order_id)

        if self.callbacks.on_sale_closed is not None:
            cb = self.callbacks.on_sale_closed

            @self._dp.on_sale_closed()
            async def handle_sale_closed(event):
                order_id = event.object.meta.order_id
                await cb(order_id)

        if self.callbacks.on_sale_refunded is not None:
            cb = self.callbacks.on_sale_refunded

            @self._dp.on_sale_refunded()
            async def handle_sale_refunded(event):
                order_id = event.object.meta.order_id
                await cb(order_id)

        if self.callbacks.on_message is not None:
            cb = self.callbacks.on_message

            @self._dp.on_new_message()
            async def handle_message(event):
                msg = event.message
                info = MessageInfo(
                    message_id=msg.id,
                    chat_id=int(msg.chat_id) if msg.chat_id else 0,
                    sender_id=msg.sender_id,
                    text=msg.text,
                    order_id=msg.meta.order_id if msg.meta else None,
                )
                await cb(info)
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_runner.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/integrations/funpay/runner.py tests/test_funpay_runner.py
git commit -m "feat: add FunPayRunner lifecycle with callback-based event handling"
```

---

## Task 13: Связка RunnerCallbacks с OrderProcessor + CommandRouter

Конструктор callback'ов: связывает OrderProcessor и CommandRouter в RunnerCallbacks для FunPayRunner. Это «glue» слой — Фаза 4 заменит заглушки на реальные сервисы.

**Files:**
- Create: `backend/app/services/funpay_lifecycle.py`
- Test: `backend/tests/test_funpay_lifecycle.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_funpay_lifecycle.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.runner import RunnerCallbacks
from app.integrations.funpay.types import MessageInfo, OrderInfo, SaleStatus
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import Lot
from app.services.funpay_lifecycle import build_callbacks


async def _seed_lot(session: AsyncSession) -> int:
    tier = SubscriptionTier(name="Plus", chatgpt_plan="plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(name="any", min_limit_pct=None, is_enabled=True)
    session.add(scope)
    await session.flush()
    lot = Lot(
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="T",
        title_en="T",
        status="active",
        auto_created=True,
    )
    session.add(lot)
    await session.flush()
    return lot.id


async def test_build_callbacks_creates_all_handlers(session: AsyncSession):
    gateway = FakeChatGateway()
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    assert isinstance(callbacks, RunnerCallbacks)
    assert callbacks.on_new_sale is not None
    assert callbacks.on_sale_closed is not None
    assert callbacks.on_sale_refunded is not None
    assert callbacks.on_message is not None


async def test_on_new_sale_callback_processes_order(session: AsyncSession):
    await _seed_lot(session)
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="ord-1",
        status=SaleStatus.PAID,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="test",
        price=599.0,
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    await callbacks.on_new_sale("ord-1")  # type: ignore
    from app.models.rental import Order
    from sqlalchemy import select
    result = await session.execute(select(Order).where(Order.funpay_order_id == "ord-1"))
    assert result.scalar_one_or_none() is not None


async def test_on_sale_closed_callback_updates_status(session: AsyncSession):
    await _seed_lot(session)
    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="ord-1",
        status=SaleStatus.COMPLETED,
        chat_id=100,
        buyer_id=200,
        subcategory_id=55,
        title="test",
        price=599.0,
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    await callbacks.on_new_sale("ord-1")  # type: ignore
    await callbacks.on_sale_closed("ord-1")  # type: ignore
    from app.models.rental import Order
    order = await session.get(Order, 1)
    assert order.status == "completed"


async def test_on_message_callback_dispatches_command(session: AsyncSession):
    gateway = FakeChatGateway()
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    msg = MessageInfo(
        message_id=1,
        chat_id=100,
        sender_id=200,
        text="!помощь",
        order_id="ord-1",
    )
    # Распознанная команда без зарегистрированного хэндлера → UnhandledMessage,
    # но lifecycle ловит и логирует (не падает)
    await callbacks.on_message(msg)  # type: ignore
    # Сообщение обработано без исключения
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_lifecycle.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/funpay_lifecycle.py`:

```python
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.integrations.funpay.runner import RunnerCallbacks
from app.integrations.funpay.types import MessageInfo
from app.services.command_router import CommandRouter, UnhandledMessage
from app.services.order_processor import OrderProcessor, LotNotFoundError

logger = logging.getLogger(__name__)


SessionFactory = Callable[[], AsyncSession]


def build_callbacks(
    session_factory: SessionFactory,
    gateway: ChatGateway,
    command_router: CommandRouter | None = None,
) -> RunnerCallbacks:
    """Сборка RunnerCallbacks из сервисов Фазы 3.

    session_factory: возвращает AsyncSession для обработки события.
    gateway: ChatGateway для вызовов FunPay.
    command_router: optional — если None, создаётся пустой (команды не обрабатываются).

    Фаза 4 расширит это: добавит AccountPool, RentalService, KickService callback'и.
    """
    order_processor = OrderProcessor()
    router = command_router or CommandRouter()

    async def on_new_sale(order_id: str) -> None:
        async with session_factory() as session:
            try:
                await order_processor.process_new_sale(session, gateway, order_id)
                await session.commit()
            except LotNotFoundError:
                logger.warning("New sale %s: no matching lot", order_id)
            except Exception:
                logger.exception("Failed to process new sale %s", order_id)

    async def on_sale_closed(order_id: str) -> None:
        async with session_factory() as session:
            try:
                await order_processor.process_sale_closed(session, order_id)
                await session.commit()
            except Exception:
                logger.exception("Failed to process sale closed %s", order_id)

    async def on_sale_refunded(order_id: str) -> None:
        async with session_factory() as session:
            try:
                await order_processor.process_sale_refunded(session, order_id)
                await session.commit()
            except Exception:
                logger.exception("Failed to process sale refunded %s", order_id)

    async def on_message(msg: MessageInfo) -> None:
        async with session_factory() as session:
            ctx = router.build_context(
                chat_id=msg.chat_id,
                sender_id=msg.sender_id or 0,
                text=msg.text or "",
                order_id=msg.order_id,
                lang="ru",
                gateway=gateway,
            )
            try:
                await router.dispatch(ctx)
                await session.commit()
            except UnhandledMessage:
                logger.debug("Unhandled command in chat %s", msg.chat_id)
            except Exception:
                logger.exception("Failed to process message in chat %s", msg.chat_id)

    return RunnerCallbacks(
        on_new_sale=on_new_sale,
        on_sale_closed=on_sale_closed,
        on_sale_refunded=on_sale_refunded,
        on_message=on_message,
    )
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_lifecycle.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay/backend
git add app/services/funpay_lifecycle.py tests/test_funpay_lifecycle.py
git commit -m "feat: add build_callbacks glue layer for FunPay events"
```

---

## Task 14: Финальная проверка — полный прогон тестов

**Files:** без изменений (только проверка)

- [ ] **Step 1: Полный прогон всех тестов**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest -v`
Expected: ALL PASS (56 из Фаз 1-2 + ~55 новых = ~111 total)

- [ ] **Step 2: Проверить отсутствие warning'ов от новых тестов**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_gateway.py tests/test_command_parser.py tests/test_command_router.py tests/test_order_processor.py tests/test_lot_sync.py tests/test_bump.py tests/test_funpay_runner.py tests/test_funpay_lifecycle.py -v 2>&1 | tail -20`
Expected: PASS, без ResourceWarning/SAWarning от новых тестов

- [ ] **Step 3: Проверить, что funpaybotengine импортируется (если установлен)**

Run: `cd /c/Source/funpay/backend && py -3.12 -c "import app.integrations.funpay.gateway; import app.integrations.funpay.runner; print('OK')"`
Expected: `OK` (если funpaybotengine установлен) или ModuleNotFoundError (если ещё не установлен — приемлемо, установка в Фазе 7)

- [ ] **Step 4: Проверить lint (опционально, если есть ruff/flake8)**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pyflakes app/integrations/funpay/ app/services/command_parser.py app/services/command_router.py app/services/order_processor.py app/services/lot_sync.py app/services/bump.py app/services/funpay_lifecycle.py 2>&1 || echo "pyflakes не установлен, пропуск"`
Expected: без ошибок (или сообщение об отсутствии pyflakes)

- [ ] **Step 5: Commit финального состояния (если были правки)**

```bash
cd /c/Source/funpay/backend
git status
# Если есть изменения — commit, иначе пропуск
```

---

## Замечания по реализации

### Граничные случаи и митигации

1. **`save_offer_fields` в FunPayChatGateway**: использует `get_offer_fields` для загрузки текущих полей перед модификацией (funpaybotengine требует существующий OfferFields объект для редактирования). Для нового лота — загружает шаблонные поля по subcategory_id. Возвращаемый offer_id для нового лота — 0 (funpaybotengine не возвращает ID созданного лота из save_offer_fields; потребуется отдельный get_my_offers для поиска по title). Это ограничение библиотеки — задокументировано в FunPayChatGateway.save_offer_fields.

2. **Идемпотентность сообщений**: FunPay может присылать дубль события. `OrderProcessor.process_new_sale` идемпотентен по funpay_order_id. Для сообщений — дедупликация будет добавлена в Фазе 4 (по message_id в AuditLog).

3. **`message.meta.order_id`**: не у всех сообщений есть order_id (обычные чаты). CommandContext.order_id будет None для не-заказных чатов — хэндлеры команд должны это учитывать.

4. **Отсутствие `complete_order`**: продавец НЕ подтверждает сделку технически — только доставляет данные через чат. Подтверждение — действие покупателя (приходит как SaleClosedEvent). Если покупатель не подтверждает — поддержка FunPay делает это через 24-48ч при наличии лога в чате.

5. **`raise_offers` бампит всю подкатегорию**: для оптимизации (не дублировать bump) BumpService.needs_bump проверяет последний bump по любой причине. В Фазе 4 Scheduler будет вызывать bump_category один раз на subcategory, а не на каждый лот.

6. **category_id для bump**: FunPayRunner хранит category_id (одна категория для всех лотов продавца). Если лоты в разных категориях — потребуется расширение (передавать category_id per-lot). Сейчас intentionally упрощено — один продавец = одна категория ChatGPT.

### Что НЕ делает Фаза 3 (intentionally отложено)

- **AccountPool.acquire()** — выдача аккаунта при заказе (Фаза 4)
- **RentalService** — создание аренды, welcome message, выдача кред (Фаза 4)
- **KickService** — logout all при истечении (Фаза 4)
- **Свежий замер лимитов перед выдачей** (Фаза 4)
- **LotAutoManager** — авто-создание/снятие лотов по capacity (Фаза 4)
- **Telegram-уведомления** (Фаза 7)
- **Реальное подключение к FunPay** (требует golden_key, Фаза 7/deployment)
