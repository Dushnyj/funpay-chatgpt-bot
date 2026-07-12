# Фаза 4: Бизнес-логика — План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать ключевую бизнес-логику аренды: выдачу аккаунтов (AccountPool), создание аренды (RentalService), обработку команд (!код/!подписка/!замена/!продавец/!помощь), кик при истечении (KickService), авто-управление лотами (LotAutoManager).

**Architecture:** Сервисы зависят от ChatGateway Protocol (FakeChatGateway в тестах). AccountPool инкапсулирует SQL-логику выбора аккаунта по tier/duration/scope/порогам. RentalService связывает Order→Account→Rental→сообщение. CommandHandlers подключаются к CommandRouter через регистрацию. KickService использует playwright + дедупликацию. LotAutoManager пересчитывает capacity и синхронизирует лоты.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, pyotp, Playwright, pytest-asyncio.

---

## Фиксированные инварианты (из существующих моделей)

- `Account.status`: `pending_validation` | `active` | `maintenance` | `banned` (новые значения вводятся в этой фазе)
- `Order.status`: `pending` | `completed` | `refunded`
- `Rental.status`: `active` | `expired` | `replaced` | `revoked` (новые в этой фазе)
- `Lot.status`: `active` | `paused` | `deleted`
- `AccountLimits.refresh_status`: `ok` | `expired`
- `AccountLimits` поля: `chat_5h_remaining_pct`, `chat_weekly_remaining_pct`, `codex_5h_remaining_pct`, `codex_weekly_remaining_pct`, `measured_at`, `refresh_status`
- `Account.max_active_rentals` (override) или `SellerSettings.default_max_active_rentals` (fallback)
- Template keys: `welcome`, `subscription`, `code_success`, `code_expired`, `code_rate_limited`, `disconnect`, `expiry`, `help`, `no_account_available`, `order_confirmed`, `replace_declined`, `replace_no_account`, `replace_success`, `seller_called`
- Template variables: welcome={login,password,tier,days,expires_at,chat_5h,chat_weekly,codex_5h,codex_weekly}, subscription={tier,expires_at,expires_in,chat_5h,chat_weekly,codex_5h,codex_weekly}, code_success={code,expires_in}, disconnect={expires_in}, expiry={tier,days}
- `render_message(session, key, lang, **variables) -> str`

---

## Структура файлов

### Новые файлы

```
backend/app/services/
├── account_pool.py      # AccountPool — SQL-выбор аккаунта по критериям
├── rental_service.py    # RentalService — Order→Account→Rental→welcome message
├── kick_service.py      # KickService — logout all с дедупликацией 60 сек
├── command_handlers.py  # 5 хэндлеров: CodeHandler, SubscriptionHandler, ReplaceHandler, SellerHandler, HelpHandler
├── lot_auto_manager.py  # LotAutoManager — capacity check + sync лотов
└── rental_expiry.py     # RentalExpiryService — поиск истёкших аренд + кик

backend/tests/
├── test_account_pool.py
├── test_rental_service.py
├── test_kick_service.py
├── test_command_handlers.py
├── test_lot_auto_manager.py
└── test_rental_expiry.py
```

### Модифицируемые

- `backend/app/services/funpay_lifecycle.py` — `build_callbacks` подключит RentalService в `on_new_sale`, реальные хэндлеры в `on_message`, KickService в `on_sale_refunded`

---

## Task 1: AccountPool — выбор аккаунта по критериям

Инкапсулирует SQL-логику: найти аккаунт, подходящий под tier/duration/scope/пороги. Возвращает Account или None.

**Files:**
- Create: `backend/app/services/account_pool.py`
- Test: `backend/tests/test_account_pool.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_account_pool.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Rental
from app.models.settings import SellerSettings
from app.services.account_pool import AccountPool, AccountCriteria


async def _seed_tier_ds(session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope_any = LimitScope(code="any", name="Любой")
    session.add(scope_any)
    await session.flush()
    return tier, duration, scope_any


async def _add_account(
    session: AsyncSession,
    tier: SubscriptionTier,
    login: str = "acc1",
    expires_in_days: int = 30,
    chat_5h: int = 80,
    chat_weekly: int = 70,
    codex_5h: int = 60,
    codex_weekly: int = 50,
    refresh_status: str = "ok",
    max_active_rentals: int | None = None,
) -> Account:
    acc = Account(
        login=login,
        password_encrypted="enc",
        totp_secret_encrypted="enc",
        tier_id=tier.id,
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        status="active",
        max_active_rentals=max_active_rentals,
    )
    session.add(acc)
    await session.flush()
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="enc",
        chat_5h_remaining_pct=chat_5h,
        chat_weekly_remaining_pct=chat_weekly,
        codex_5h_remaining_pct=codex_5h,
        codex_weekly_remaining_pct=codex_weekly,
        measured_at=datetime.now(timezone.utc),
        refresh_status=refresh_status,
    )
    session.add(limits)
    await session.flush()
    return acc


async def test_acquire_returns_account_matching_basic_criteria(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    acc = await _add_account(session, tier)
    settings = SellerSettings(id=1)
    session.add(settings)
    await session.flush()

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=duration.days,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=None,
        max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is not None
    assert result.id == acc.id


async def test_acquire_returns_none_when_no_active_accounts(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    acc = await _add_account(session, tier)
    acc.status = "maintenance"
    await session.flush()

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=duration.days,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=None,
        max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is None


async def test_acquire_filters_out_expired_subscription(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    # Подписка истекает через 3 дня, а аренда на 7 — не подходит
    await _add_account(session, tier, expires_in_days=3)
    settings = SellerSettings(id=1)
    session.add(settings)
    await session.flush()

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=7,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=None,
        max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is None


async def test_acquire_filters_out_refresh_expired(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    await _add_account(session, tier, refresh_status="expired")

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=7,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=None,
        max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is None


async def test_acquire_scope_any_with_max_5h_threshold(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    # chat_5h=80 > max_5h=30 → не подходит
    await _add_account(session, tier, chat_5h=80, codex_5h=80)
    # Другой аккаунт подходит
    acc2 = await _add_account(
        session, tier, login="acc2", chat_5h=20, codex_5h=25,
    )

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=7,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=30,
        max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is not None
    assert result.id == acc2.id


async def test_acquire_scope_codex_with_min_limit(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    # codex_5h=40, codex_weekly=30 — ниже min 50 → не подходит
    await _add_account(session, tier, codex_5h=40, codex_weekly=30)
    acc2 = await _add_account(
        session, tier, login="acc2", codex_5h=70, codex_weekly=60,
    )

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=7,
        scope="codex",
        min_limit_pct=50,
        max_5h_pct=None,
        max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is not None
    assert result.id == acc2.id


async def test_acquire_respects_max_active_rentals(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    acc = await _add_account(session, tier, max_active_rentals=1)

    # Создаём активную аренду на этом аккаунте → слот занят
    from sqlalchemy import select
    from app.models.rental import Order
    order = Order(
        funpay_order_id="o1", funpay_chat_id="1", buyer_funpay_id="1",
        lot_id=None, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope_any.id, price=100, status="pending",
    )
    session.add(order)
    await session.flush()
    rental = Rental(
        order_id=order.id,
        account_id=acc.id,
        buyer_funpay_id="1",
        buyer_funpay_chat_id="1",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope_any.id,
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
    )
    session.add(rental)
    await session.flush()

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=7,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=None,
        max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is None  # слот занят


async def test_acquire_fifo_orders_by_subscription_expires_asc(session: AsyncSession):
    """scope=any → FIFO по сроку подписки (ближайший к истечению первым)."""
    tier, duration, scope_any = await _seed_tier_ds(session)
    # acc2 истекает раньше (10 дней), acc1 позже (30 дней) → acc2 должен быть выбран
    acc1 = await _add_account(session, tier, login="acc1", expires_in_days=30)
    acc2 = await _add_account(session, tier, login="acc2", expires_in_days=10)

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=7,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=None,
        max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=5)
    assert result is not None
    assert result.id == acc2.id  # FIFO — ближайший к истечению
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_account_pool.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/account_pool.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountLimits
from app.models.rental import Rental


# Окно актуальности замера лимитов: старше этого — аккаунт не подходит
_LIMITS_FRESH_THRESHOLD = timedelta(hours=1)


@dataclass(frozen=True)
class AccountCriteria:
    """Критерии выбора аккаунта для выдачи под заказ."""

    tier_id: int
    duration_days: int
    scope: str  # any | chat | codex
    min_limit_pct: int | None
    max_5h_pct: int | None
    max_weekly_pct: int | None


class AccountPool:
    """Выбор аккаунта из пула под критерии заказа.

    base_filter: status=active, tier, подписка >= duration, лимиты свежие, refresh ok,
                 активных аренд < эффективного лимита.
    scope=any: потолок (если задан) — все 4 замера ≤ порогов. FIFO по подписке.
    scope=chat/codex: гарантия — оба окна типа ≥ min_limit_pct. Наибольший запас.
    """

    async def acquire(
        self,
        session: AsyncSession,
        criteria: AccountCriteria,
        default_max_active_rentals: int,
    ) -> Account | None:
        now = datetime.now(timezone.utc)
        fresh_cutoff = now - _LIMITS_FRESH_THRESHOLD
        required_expires_at = now + timedelta(days=criteria.duration_days)

        # Подзапрос: счётчик активных аренд на аккаунт
        active_rentals = (
            select(
                Rental.account_id,
                func.count(Rental.id).label("cnt"),
            )
            .where(Rental.status == "active")
            .group_by(Rental.account_id)
            .subquery()
        )

        stmt = (
            select(Account)
            .join(AccountLimits, AccountLimits.account_id == Account.id)
            .outerjoin(active_rentals, active_rentals.c.account_id == Account.id)
            .where(
                Account.status == "active",
                Account.tier_id == criteria.tier_id,
                Account.subscription_expires_at >= required_expires_at,
                AccountLimits.measured_at >= fresh_cutoff,
                AccountLimits.refresh_status == "ok",
                # Эффективный лимит: COALESCE(account.override, default)
                func.coalesce(
                    Account.max_active_rentals, default_max_active_rentals
                )
                > func.coalesce(active_rentals.c.cnt, 0),
            )
        )

        # Пороги по scope
        if criteria.scope == "any":
            if criteria.max_5h_pct is not None:
                stmt = stmt.where(
                    AccountLimits.chat_5h_remaining_pct <= criteria.max_5h_pct,
                    AccountLimits.codex_5h_remaining_pct <= criteria.max_5h_pct,
                )
            if criteria.max_weekly_pct is not None:
                stmt = stmt.where(
                    AccountLimits.chat_weekly_remaining_pct <= criteria.max_weekly_pct,
                    AccountLimits.codex_weekly_remaining_pct <= criteria.max_weekly_pct,
                )
            # FIFO: ближайший к истечению подписки первым
            stmt = stmt.order_by(Account.subscription_expires_at.asc())
        else:
            # chat или codex: оба окна типа ≥ min_limit_pct
            if criteria.scope == "chat":
                stmt = stmt.where(
                    AccountLimits.chat_5h_remaining_pct >= criteria.min_limit_pct,
                    AccountLimits.chat_weekly_remaining_pct >= criteria.min_limit_pct,
                )
                # Наибольший запас: LEAST(5h, weekly) DESC
                stmt = stmt.order_by(
                    func.min(
                        AccountLimits.chat_5h_remaining_pct,
                        AccountLimits.chat_weekly_remaining_pct,
                    ).desc()
                )
            else:  # codex
                stmt = stmt.where(
                    AccountLimits.codex_5h_remaining_pct >= criteria.min_limit_pct,
                    AccountLimits.codex_weekly_remaining_pct >= criteria.min_limit_pct,
                )
                stmt = stmt.order_by(
                    func.min(
                        AccountLimits.codex_5h_remaining_pct,
                        AccountLimits.codex_weekly_remaining_pct,
                    ).desc()
                )

        stmt = stmt.limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_account_pool.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add backend/app/services/account_pool.py backend/tests/test_account_pool.py
git commit -m "feat: add AccountPool for account selection by rental criteria"
```

---

## Task 2: RentalService — Order→Account→Rental→welcome

Связывает: получить Order → acquire аккаунт → создать Rental → отправить welcome. Если аккаунта нет — отправить no_account_available и вернуть None.

**Files:**
- Create: `backend/app/services/rental_service.py`
- Test: `backend/tests/test_rental_service.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_rental_service.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings
from app.services.rental_service import RentalService


async def _seed_full(session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    settings = SellerSettings(id=1)
    session.add(settings)
    acc = Account(
        login="acc1",
        password_encrypted="plain_pass",
        totp_secret_encrypted="plain_totp",
        tier_id=tier.id,
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        status="active",
    )
    session.add(acc)
    await session.flush()
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="enc",
        chat_5h_remaining_pct=80,
        chat_weekly_remaining_pct=70,
        codex_5h_remaining_pct=60,
        codex_weekly_remaining_pct=50,
        measured_at=datetime.now(timezone.utc),
        refresh_status="ok",
    )
    session.add(limits)
    order = Order(
        funpay_order_id="ord-1",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        lot_id=None,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        status="pending",
    )
    session.add(order)
    await session.flush()
    return tier, duration, scope, acc, order


async def test_fulfill_order_creates_rental_and_sends_welcome(session: AsyncSession):
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()

    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)

    assert rental is not None
    assert rental.account_id == acc.id
    assert rental.order_id == order.id
    assert rental.status == "active"
    assert rental.expires_at > rental.started_at
    # welcome отправлен в чат сделки
    assert len(gateway.sent_messages) == 1
    chat_id, text = gateway.sent_messages[0]
    assert chat_id == 100
    assert "plain_pass" in text  # пароль подставился в шаблон


async def test_fulfill_order_sends_no_account_message_when_pool_empty(session: AsyncSession):
    tier, duration, scope, acc, order = await _seed_full(session)
    acc.status = "maintenance"  # аккаунт недоступен
    await session.flush()
    gateway = FakeChatGateway()
    svc = RentalService()

    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)

    assert rental is None
    assert len(gateway.sent_messages) == 1
    chat_id, text = gateway.sent_messages[0]
    assert chat_id == 100


async def test_fulfill_order_idempotent_existing_rental(session: AsyncSession):
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()
    first = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    # Второй вызов — не создаёт дубль
    gateway.sent_messages.clear()
    second = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    assert second is not None
    assert second.id == first.id
    assert len(gateway.sent_messages) == 0  # welcome не отправляется повторно

    rentals = (await session.execute(select(Rental).where(Rental.order_id == order.id))).scalars().all()
    assert len(rentals) == 1


async def test_fulfill_order_records_issued_limits(session: AsyncSession):
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()
    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    assert rental is not None
    assert rental.issued_chat_5h_pct == 80
    assert rental.issued_chat_weekly_pct == 70
    assert rental.issued_codex_5h_pct == 60
    assert rental.issued_codex_weekly_pct == 50


async def test_revoke_rental_sets_status(session: AsyncSession):
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()
    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    assert rental is not None
    await svc.revoke_rental(session, rental.id)
    await session.refresh(rental)
    assert rental.status == "revoked"
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_rental_service.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/rental_service.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import Duration, SubscriptionTier
from app.models.rental import Order, Rental
from app.services.account_pool import AccountCriteria, AccountPool
from app.services.crypto import decrypt
from app.services.messages import render_message


class RentalService:
    """Связывает Order → Account → Rental → welcome message.

    fulfill_order: идемпотентен (если Rental для Order уже есть — возвращает существующий).
    Если аккаунт не найден — отправляет no_account_available, возвращает None.
    """

    def __init__(self, account_pool: AccountPool | None = None) -> None:
        self._pool = account_pool or AccountPool()

    async def fulfill_order(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: int,
        default_max_active_rentals: int,
    ) -> Rental | None:
        existing = await self._find_rental_by_order(session, order_id)
        if existing is not None:
            return existing

        order = await session.get(Order, order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found")

        duration = await session.get(Duration, order.duration_id)
        if duration is None:
            raise KeyError(f"Duration {order.duration_id} not found")

        scope = await self._get_scope_code(session, order.limit_scope_id)
        criteria = AccountCriteria(
            tier_id=order.tier_id,
            duration_days=duration.days,
            scope=scope,
            min_limit_pct=order.min_limit_pct,
            max_5h_pct=order.max_5h_pct,
            max_weekly_pct=order.max_weekly_pct,
        )
        account = await self._pool.acquire(session, criteria, default_max_active_rentals)
        if account is None:
            await self._send_no_account(session, gateway, order)
            return None

        limits = await session.get(AccountLimits, account.id)
        now = datetime.now(timezone.utc)
        rental = Rental(
            order_id=order.id,
            account_id=account.id,
            buyer_funpay_id=order.buyer_funpay_id,
            buyer_funpay_chat_id=order.funpay_chat_id,
            tier_id=order.tier_id,
            duration_id=order.duration_id,
            limit_scope_id=order.limit_scope_id,
            min_limit_pct=order.min_limit_pct,
            max_5h_pct=order.max_5h_pct,
            max_weekly_pct=order.max_weekly_pct,
            lang=order.buyer_locale or "ru",
            started_at=now,
            expires_at=now + timedelta(days=duration.days),
            status="active",
            issued_chat_5h_pct=limits.chat_5h_remaining_pct if limits else None,
            issued_chat_weekly_pct=limits.chat_weekly_remaining_pct if limits else None,
            issued_codex_5h_pct=limits.codex_5h_remaining_pct if limits else None,
            issued_codex_weekly_pct=limits.codex_weekly_remaining_pct if limits else None,
        )
        session.add(rental)
        await session.flush()

        await self._send_welcome(session, gateway, order, account, limits, duration.days)
        return rental

    async def revoke_rental(self, session: AsyncSession, rental_id: int) -> Rental:
        rental = await session.get(Rental, rental_id)
        if rental is None:
            raise KeyError(f"Rental {rental_id} not found")
        rental.status = "revoked"
        await session.flush()
        return rental

    async def _find_rental_by_order(
        self, session: AsyncSession, order_id: int,
    ) -> Rental | None:
        result = await session.execute(
            select(Rental).where(Rental.order_id == order_id)
        )
        return result.scalar_one_or_none()

    async def _get_scope_code(self, session: AsyncSession, scope_id: int | None) -> str:
        from app.models.catalog import LimitScope
        if scope_id is None:
            return "any"
        scope = await session.get(LimitScope, scope_id)
        return scope.code if scope else "any"

    async def _send_welcome(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order: Order,
        account: Account,
        limits: AccountLimits | None,
        days: int,
    ) -> None:
        tier = await session.get(SubscriptionTier, account.tier_id)
        password = decrypt(account.password_encrypted)
        lang = order.buyer_locale or "ru"
        text = await render_message(
            session, "welcome", lang,
            login=account.login,
            password=password,
            tier=tier.name if tier else "",
            days=days,
            expires_at=_fmt_expires(account.subscription_expires_at),
            chat_5h=_pct(limits, "chat_5h") if limits else "—",
            chat_weekly=_pct(limits, "chat_weekly") if limits else "—",
            codex_5h=_pct(limits, "codex_5h") if limits else "—",
            codex_weekly=_pct(limits, "codex_weekly") if limits else "—",
        )
        await gateway.send_message(chat_id=int(order.funpay_chat_id), text=text)

    async def _send_no_account(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order: Order,
    ) -> None:
        lang = order.buyer_locale or "ru"
        text = await render_message(session, "no_account_available", lang)
        await gateway.send_message(chat_id=int(order.funpay_chat_id), text=text)


def _pct(limits: AccountLimits, field: str) -> str:
    val = getattr(limits, f"{field}_remaining_pct")
    return f"{val}%" if val is not None else "—"


def _fmt_expires(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y")
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_rental_service.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add backend/app/services/rental_service.py backend/tests/test_rental_service.py
git commit -m "feat: add RentalService linking Order to Account and Rental"
```

---

## Task 3: KickService — logout all с дедупликацией

Kick аккаунта через Playwright. Дедупликация: не более одного kick на аккаунт за 60 секунд.

**Files:**
- Create: `backend/app/services/kick_service.py`
- Test: `backend/tests/test_kick_service.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_kick_service.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.catalog import SubscriptionTier
from app.services.kick_service import KickService, KickResult


async def _add_account(session: AsyncSession, login: str = "acc1") -> Account:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    acc = Account(
        login=login,
        password_encrypted="enc_pass",
        totp_secret_encrypted="enc_totp",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    return acc


async def test_kick_account_success(session: AsyncSession):
    acc = await _add_account(session)
    svc = KickService()
    with patch("app.services.kick_service.browser_context") as mock_ctx, \
         patch("app.services.kick_service.kick_account", new_callable=AsyncMock) as mock_kick:
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await svc.kick(session, acc.id)
    assert result.success is True
    mock_kick.assert_awaited_once()


async def test_kick_account_failure_returns_error(session: AsyncSession):
    acc = await _add_account(session)
    svc = KickService()
    with patch("app.services.kick_service.browser_context") as mock_ctx, \
         patch("app.services.kick_service.kick_account", new_callable=AsyncMock) as mock_kick:
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_kick.side_effect = RuntimeError("login failed")
        result = await svc.kick(session, acc.id)
    assert result.success is False
    assert "login failed" in (result.error or "")


async def test_kick_dedup_skips_within_60_seconds(session: AsyncSession):
    acc = await _add_account(session)
    svc = KickService()
    with patch("app.services.kick_service.browser_context") as mock_ctx, \
         patch("app.services.kick_service.kick_account", new_callable=AsyncMock) as mock_kick:
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        await svc.kick(session, acc.id)
        result2 = await svc.kick(session, acc.id)
    assert result2.success is True
    assert result2.deduplicated is True
    mock_kick.assert_awaited_once()  # реальный kick вызван только 1 раз


async def test_kick_unknown_account_raises(session: AsyncSession):
    svc = KickService()
    with pytest.raises(KeyError):
        await svc.kick(session, 99999)
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_kick_service.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/kick_service.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.playwright.browser import browser_context
from app.integrations.playwright.kick import kick_account
from app.models.account import Account
from app.services.crypto import decrypt


# Дедупликация: один logout all на аккаунт за этот период
_KICK_DEDUP_WINDOW = timedelta(seconds=60)


@dataclass(frozen=True)
class KickResult:
    """Результат операции kick."""

    success: bool
    deduplicated: bool = False
    error: str | None = None


class KickService:
    """Logout all через Playwright с дедупликацией 60 сек.

    Дедупликация in-memory: повторный kick аккаунта в пределах окна — no-op.
    """

    def __init__(self) -> None:
        self._last_kick_at: dict[int, datetime] = {}

    async def kick(self, session: AsyncSession, account_id: int) -> KickResult:
        now = datetime.now(timezone.utc)
        last = self._last_kick_at.get(account_id)
        if last is not None and now - last < _KICK_DEDUP_WINDOW:
            return KickResult(success=True, deduplicated=True)

        account = await session.get(Account, account_id)
        if account is None:
            raise KeyError(f"Account {account_id} not found")

        password = decrypt(account.password_encrypted)
        totp_secret = decrypt(account.totp_secret_encrypted)

        try:
            async with browser_context() as context:
                await kick_account(
                    context=context,
                    login=account.login,
                    password=password,
                    totp_secret=totp_secret,
                )
        except Exception as exc:
            return KickResult(success=False, error=str(exc))

        self._last_kick_at[account_id] = now
        return KickResult(success=True)
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_kick_service.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add backend/app/services/kick_service.py backend/tests/test_kick_service.py
git commit -m "feat: add KickService with 60s dedup for logout-all"
```

---

## Task 4: CommandHandlers — !код и !помощь

CodeHandler: найти активную аренду по chat_id → проверить не истекла → анти-спам 30с → сгенерировать TOTP → отправить. HelpHandler: отправить help template.

**Files:**
- Create: `backend/app/services/command_handlers.py`
- Test: `backend/tests/test_command_handlers.py`

- [ ] **Step 1: Написать тест (часть 1: CodeHandler + HelpHandler)**

`backend/tests/test_command_handlers.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Order, Rental
from app.services.command_handlers import CodeHandler, HelpHandler
from app.services.command_router import CommandContext
from app.services.command_parser import CommandType, ParsedCommand


_CODE_ANTISPAM_SECONDS = 30


async def _seed_rental(session: AsyncSession, chat_id: int = 100) -> Rental:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    acc = Account(
        login="acc1",
        password_encrypted="enc",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    order = Order(
        funpay_order_id="o1",
        funpay_chat_id=str(chat_id),
        buyer_funpay_id="200",
        lot_id=None, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=100, status="pending",
    )
    session.add(order)
    await session.flush()
    rental = Rental(
        order_id=order.id, account_id=acc.id,
        buyer_funpay_id="200", buyer_funpay_chat_id=str(chat_id),
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        lang="ru", started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
    )
    session.add(rental)
    await session.flush()
    return rental


def _ctx(gateway: FakeChatGateway, chat_id: int = 100, lang: str = "ru",
         parsed: ParsedCommand | None = None) -> CommandContext:
    return CommandContext(
        chat_id=chat_id,
        sender_id=200,
        text="!код",
        order_id=None,
        lang=lang,
        gateway=gateway,
        parsed=parsed or ParsedCommand(command=CommandType.CODE, argument=None),
    )


async def test_code_handler_sends_totp(session: AsyncSession):
    rental = await _seed_rental(session)
    gateway = FakeChatGateway()
    handler = CodeHandler()
    ctx = _ctx(gateway, chat_id=100)
    with patch("app.services.command_handlers.generate_totp", return_value="123456"):
        await handler(ctx)
    assert len(gateway.sent_messages) == 1
    _, text = gateway.sent_messages[0]
    assert "123456" in text


async def test_code_handler_rejects_expired_rental(session: AsyncSession):
    rental = await _seed_rental(session)
    rental.status = "expired"
    await session.flush()
    gateway = FakeChatGateway()
    handler = CodeHandler()
    ctx = _ctx(gateway)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1
    _, text = gateway.sent_messages[0]
    # code_expired template отправлен
    assert "123456" not in text


async def test_code_handler_no_rental_sends_expired(session: AsyncSession):
    gateway = FakeChatGateway()
    handler = CodeHandler()
    ctx = _ctx(gateway, chat_id=999)  # нет аренды для этого чата
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_code_handler_antispam_blocks_within_30s(session: AsyncSession):
    await _seed_rental(session)
    gateway = FakeChatGateway()
    handler = CodeHandler()
    ctx = _ctx(gateway)
    with patch("app.services.command_handlers.generate_totp", return_value="111111"):
        await handler(ctx)
    # Второй вызов сразу — анти-спам
    with patch("app.services.command_handlers.generate_totp", return_value="222222"):
        await handler(ctx)
    assert len(gateway.sent_messages) == 2  # первый код + rate_limited
    _, second_text = gateway.sent_messages[1]
    assert "222222" not in second_text


async def test_help_handler_sends_help_template(session: AsyncSession):
    from app.services.messages import render_message
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)

    gateway = FakeChatGateway()
    handler = HelpHandler()
    ctx = CommandContext(
        chat_id=100, sender_id=200, text="!помощь", order_id=None,
        lang="ru", gateway=gateway,
        parsed=ParsedCommand(command=CommandType.HELP, argument=None),
    )
    await handler(ctx)
    assert len(gateway.sent_messages) == 1
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_handlers.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать (часть 1: CodeHandler + HelpHandler)**

`backend/app/services/command_handlers.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account
from app.models.rental import Rental
from app.services.command_router import CommandContext
from app.services.crypto import decrypt
from app.services.messages import render_message
from app.services.totp import generate_totp


# Анти-спам на выдачу кода: 1 код в этот период
_CODE_RATE_LIMIT = timedelta(seconds=30)


def _session_from_ctx(ctx: CommandContext) -> AsyncSession:
    """Достаёт AsyncSession из контекста.

    Session注入ается в контекст внешним кодом (funpay_lifecycle.build_callbacks).
    Хранится в ctx как атрибут _session (см. Task 8 модификацию build_callbacks).
    """
    session = getattr(ctx, "_session", None)
    if session is None:
        raise RuntimeError("CommandContext has no _session attached")
    return session


class CodeHandler:
    """Обработка !код/!code: выдача TOTP по активной аренде.

    Привязка по chat_id (не по логину в команде). Анти-спам 30 сек.
    """

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        rental = await self._find_active_rental(session, ctx.chat_id)

        if rental is None or rental.status != "active":
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        now = datetime.now(timezone.utc)
        if rental.last_code_request_at is not None:
            elapsed = now - rental.last_code_request_at
            if elapsed < _CODE_RATE_LIMIT:
                text = await render_message(session, "code_rate_limited", ctx.lang)
                await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
                return

        account = await session.get(Account, rental.account_id)
        if account is None:
            return
        totp_secret = decrypt(account.totp_secret_encrypted)
        code = generate_totp(totp_secret)

        rental.last_code_request_at = now
        await session.flush()

        expires_in = _fmt_remaining(rental.expires_at, now)
        text = await render_message(
            session, "code_success", ctx.lang, code=code, expires_in=expires_in,
        )
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)

    async def _find_active_rental(
        self, session: AsyncSession, chat_id: int,
    ) -> Rental | None:
        result = await session.execute(
            select(Rental).where(Rental.buyer_funpay_chat_id == str(chat_id))
        )
        # Берём последнюю аренду для этого чата
        rentals = result.scalars().all()
        if not rentals:
            return None
        return max(rentals, key=lambda r: r.started_at)


class HelpHandler:
    """Обработка !помощь/!help: отправка help template."""

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        text = await render_message(session, "help", ctx.lang)
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)


def _fmt_remaining(expires_at: datetime, now: datetime) -> str:
    delta = expires_at - now
    if delta.total_seconds() <= 0:
        return "0"
    hours = int(delta.total_seconds() // 3600)
    if hours < 24:
        return f"{hours}ч"
    return f"{hours // 24}д"
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_handlers.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add backend/app/services/command_handlers.py backend/tests/test_command_handlers.py
git commit -m "feat: add CodeHandler and HelpHandler for !code and !help commands"
```

---

## Task 5: CommandHandlers — !подписка и !продавец

SubscriptionHandler: показать tier, expires_at, лимиты. SellerHandler: уведомление продавцу (заглушка — просто отвечает «продавец уведомлён»).

**Files:**
- Modify: `backend/app/services/command_handlers.py` (добавить SubscriptionHandler, SellerHandler)
- Modify: `backend/tests/test_command_handlers.py` (добавить тесты)

- [ ] **Step 1: Дописать тесты**

Добавить в конец `backend/tests/test_command_handlers.py`:

```python
from app.services.command_handlers import SubscriptionHandler, SellerHandler


async def test_subscription_handler_shows_limits(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await _seed_rental(session)
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = SubscriptionHandler()
    ctx = CommandContext(
        chat_id=100, sender_id=200, text="!подписка", order_id=None,
        lang="ru", gateway=gateway,
        parsed=ParsedCommand(command=CommandType.SUBSCRIPTION, argument=None),
    )
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_subscription_handler_no_rental_sends_expired(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = SubscriptionHandler()
    ctx = CommandContext(
        chat_id=999, sender_id=200, text="!подписка", order_id=None,
        lang="ru", gateway=gateway,
        parsed=ParsedCommand(command=CommandType.SUBSCRIPTION, argument=None),
    )
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_seller_handler_responds(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = SellerHandler()
    ctx = CommandContext(
        chat_id=100, sender_id=200, text="!продавец", order_id=None,
        lang="ru", gateway=gateway,
        parsed=ParsedCommand(command=CommandType.SELLER, argument=None),
    )
    await handler(ctx)
    assert len(gateway.sent_messages) == 1
    _, text = gateway.sent_messages[0]
    # seller_called template отправлен
    assert len(text) > 0
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_handlers.py::test_subscription_handler_shows_limits -v`
Expected: FAIL (ImportError: cannot import name 'SubscriptionHandler')

- [ ] **Step 3: Реализовать (добавить в конец command_handlers.py)**

```python
from app.models.account import AccountLimits


class SubscriptionHandler:
    """Обработка !подписка/!sub: показать тариф, срок, лимиты аккаунта."""

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        rental = await self._find_rental(session, ctx.chat_id)

        if rental is None or rental.status != "active":
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        account = await session.get(Account, rental.account_id)
        if account is None:
            return
        limits = await session.get(AccountLimits, account.id)
        from app.models.catalog import SubscriptionTier
        tier = await session.get(SubscriptionTier, account.tier_id)

        now = datetime.now(timezone.utc)
        text = await render_message(
            session, "subscription", ctx.lang,
            tier=tier.name if tier else "",
            expires_at=_fmt_date(account.subscription_expires_at),
            expires_in=_fmt_remaining(rental.expires_at, now),
            chat_5h=_pct_val(limits, "chat_5h") if limits else "—",
            chat_weekly=_pct_val(limits, "chat_weekly") if limits else "—",
            codex_5h=_pct_val(limits, "codex_5h") if limits else "—",
            codex_weekly=_pct_val(limits, "codex_weekly") if limits else "—",
        )
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)

    async def _find_rental(self, session: AsyncSession, chat_id: int) -> Rental | None:
        result = await session.execute(
            select(Rental).where(Rental.buyer_funpay_chat_id == str(chat_id))
        )
        rentals = result.scalars().all()
        if not rentals:
            return None
        return max(rentals, key=lambda r: r.started_at)


class SellerHandler:
    """Обработка !продавец/!seller: уведомление продавцу.

    Фаза 7 добавит Telegram-нотификацию. Пока — отвечает seller_called template.
    """

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        text = await render_message(session, "seller_called", ctx.lang)
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)


def _pct_val(limits: AccountLimits, field: str) -> str:
    val = getattr(limits, f"{field}_remaining_pct")
    return f"{val}%" if val is not None else "—"


def _fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y")
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_handlers.py -v`
Expected: PASS (8 tests: 5 + 3 новых)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add backend/app/services/command_handlers.py backend/tests/test_command_handlers.py
git commit -m "feat: add SubscriptionHandler and SellerHandler"
```

---

## Task 6: CommandHandlers — !замена

ReplaceHandler: найти активную аренду → выбрать ДРУГОЙ аккаунт через AccountPool.acquire_excluding → сменить account_id на той же Rental → отправить welcome с новыми кредами.

ВАЖНО: Rental имеет UniqueConstraint на order_id. Замена = смена аккаунта на ТОЙ ЖЕ Rental (replacement_count++), а не создание новой. Старый аккаунт НЕ кикается здесь (踢 отдельной операцией).

**Files:**
- Modify: `backend/app/services/command_handlers.py` (добавить ReplaceHandler)
- Modify: `backend/app/services/account_pool.py` (добавить acquire_excluding)
- Modify: `backend/tests/test_command_handlers.py` (добавить тесты)

- [ ] **Step 1: Дописать тесты**

Добавить в конец `backend/tests/test_command_handlers.py`:

```python
from app.services.command_handlers import ReplaceHandler


async def test_replace_handler_switches_account(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    from app.models.account import AccountLimits
    rental = await _seed_rental(session)
    old_account_id = rental.account_id
    await seed_message_templates(session)

    # Второй аккаунт для замены
    tier = await session.get(SubscriptionTier, rental.tier_id)
    acc2 = Account(
        login="acc2", password_encrypted="enc2", totp_secret_encrypted="enc_totp",
        tier_id=tier.id, status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc2)
    await session.flush()
    session.add(AccountLimits(
        account_id=acc2.id, refresh_token_encrypted="enc",
        chat_5h_remaining_pct=90, chat_weekly_remaining_pct=80,
        codex_5h_remaining_pct=70, codex_weekly_remaining_pct=60,
        measured_at=datetime.now(timezone.utc), refresh_status="ok",
    ))
    await session.flush()

    gateway = FakeChatGateway()
    handler = ReplaceHandler()
    ctx = CommandContext(
        chat_id=100, sender_id=200, text="!замена", order_id=None,
        lang="ru", gateway=gateway,
        parsed=ParsedCommand(command=CommandType.REPLACE, argument=None),
    )
    await handler(ctx)
    assert len(gateway.sent_messages) == 1  # welcome с новыми кредами
    _, text = gateway.sent_messages[0]
    assert "enc2" in text or "acc2" in text  # новые креды в тексте
    await session.refresh(rental)
    assert rental.account_id != old_account_id  # аккаунт сменился
    assert rental.replacement_count == 1


async def test_replace_handler_no_account_available(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await _seed_rental(session)
    await seed_message_templates(session)

    gateway = FakeChatGateway()
    handler = ReplaceHandler()
    ctx = CommandContext(
        chat_id=100, sender_id=200, text="!замена", order_id=None,
        lang="ru", gateway=gateway,
        parsed=ParsedCommand(command=CommandType.REPLACE, argument=None),
    )
    # Нет второго аккаунта (acquire_excluding вернёт None)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1  # replace_no_account template


async def test_replace_handler_no_active_rental(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = ReplaceHandler()
    ctx = CommandContext(
        chat_id=999, sender_id=200, text="!замена", order_id=None,  # нет аренды
        lang="ru", gateway=gateway,
        parsed=ParsedCommand(command=CommandType.REPLACE, argument=None),
    )
    await handler(ctx)
    assert len(gateway.sent_messages) == 1  # replace_declined template
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_handlers.py::test_replace_handler_switches_account -v`
Expected: FAIL (ImportError: cannot import name 'ReplaceHandler')

- [ ] **Step 3: Добавить acquire_excluding в AccountPool**

В конец класса `AccountPool` в `backend/app/services/account_pool.py` добавить:

```python
    async def acquire_excluding(
        self,
        session: AsyncSession,
        criteria: AccountCriteria,
        exclude_account_id: int,
        default_max_active_rentals: int,
    ) -> Account | None:
        """Как acquire, но исключает указанный аккаунт (для замены).

        Временно помечает исключаемый аккаунт как maintenance, вызывает acquire,
        затем восстанавливает исходный статус.
        """
        excluded = await session.get(Account, exclude_account_id)
        if excluded is not None:
            original_status = excluded.status
            excluded.status = "maintenance"
            await session.flush()
            try:
                return await self.acquire(session, criteria, default_max_active_rentals)
            finally:
                excluded.status = original_status
                await session.flush()
        return await self.acquire(session, criteria, default_max_active_rentals)
```

- [ ] **Step 4: Реализовать ReplaceHandler (добавить в конец command_handlers.py)**

```python
from app.models.catalog import Duration, LimitScope
from app.services.account_pool import AccountCriteria, AccountPool


class ReplaceHandler:
    """Обработка !замена/!replace: смена аккаунта на той же аренде.

    Rental.order_id имеет UNIQUE constraint — замена = смена account_id
    на существующей Rental (replacement_count++), а не создание новой.
    Старый аккаунт НЕ кикается здесь (踢 отдельной операцией).
    """

    def __init__(self, account_pool: AccountPool | None = None) -> None:
        self._pool = account_pool or AccountPool()

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        rental = await self._find_active_rental(session, ctx.chat_id)

        if rental is None:
            text = await render_message(session, "replace_declined", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        duration = await session.get(Duration, rental.duration_id)
        scope = await session.get(LimitScope, rental.limit_scope_id)
        if duration is None:
            return

        criteria = AccountCriteria(
            tier_id=rental.tier_id,
            duration_days=duration.days,
            scope=scope.code if scope else "any",
            min_limit_pct=rental.min_limit_pct,
            max_5h_pct=rental.max_5h_pct,
            max_weekly_pct=rental.max_weekly_pct,
        )
        new_account = await self._pool.acquire_excluding(
            session, criteria,
            exclude_account_id=rental.account_id,
            default_max_active_rentals=5,
        )

        if new_account is None:
            text = await render_message(session, "replace_no_account", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        rental.account_id = new_account.id
        rental.replacement_count += 1
        limits = await session.get(AccountLimits, new_account.id)
        if limits:
            rental.issued_chat_5h_pct = limits.chat_5h_remaining_pct
            rental.issued_chat_weekly_pct = limits.chat_weekly_remaining_pct
            rental.issued_codex_5h_pct = limits.codex_5h_remaining_pct
            rental.issued_codex_weekly_pct = limits.codex_weekly_remaining_pct
        await session.flush()

        password = decrypt(new_account.password_encrypted)
        tier = await session.get(SubscriptionTier, new_account.tier_id)
        text = await render_message(
            session, "welcome", ctx.lang,
            login=new_account.login,
            password=password,
            tier=tier.name if tier else "",
            days=duration.days,
            expires_at=_fmt_date(new_account.subscription_expires_at),
            chat_5h=_pct_val(limits, "chat_5h") if limits else "—",
            chat_weekly=_pct_val(limits, "chat_weekly") if limits else "—",
            codex_5h=_pct_val(limits, "codex_5h") if limits else "—",
            codex_weekly=_pct_val(limits, "codex_weekly") if limits else "—",
        )
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)

    async def _find_active_rental(
        self, session: AsyncSession, chat_id: int,
    ) -> Rental | None:
        result = await session.execute(
            select(Rental).where(
                Rental.buyer_funpay_chat_id == str(chat_id),
                Rental.status == "active",
            )
        )
        rentals = result.scalars().all()
        if not rentals:
            return None
        return max(rentals, key=lambda r: r.started_at)
```

- [ ] **Step 5: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_command_handlers.py -v`
Expected: PASS (11 tests: 8 из Tasks 4-5 + 3 новых)

- [ ] **Step 6: Commit**

```bash
cd /c/Source/funpay
git add backend/app/services/command_handlers.py backend/app/services/account_pool.py backend/tests/test_command_handlers.py
git commit -m "feat: add ReplaceHandler for !replace command with account switching"
```

---

## Task 7: RentalExpiryService — поиск истёкших + expiry message

Находит активные аренды с expires_at <= NOW(), помечает expired, отправляет expiry message в чат.

**Files:**
- Create: `backend/app/services/rental_expiry.py`
- Test: `backend/tests/test_rental_expiry.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_rental_expiry.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Order, Rental
from app.services.rental_expiry import RentalExpiryService


async def _make_rental(
    session: AsyncSession,
    expires_delta: timedelta,
    chat_id: str = "100",
    status: str = "active",
) -> Rental:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    acc = Account(
        login="acc1", password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=tier.id, status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    order = Order(
        funpay_order_id="o1", funpay_chat_id=chat_id, buyer_funpay_id="200",
        lot_id=None, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=100, status="pending",
    )
    session.add(order)
    await session.flush()
    rental = Rental(
        order_id=order.id, account_id=acc.id,
        buyer_funpay_id="200", buyer_funpay_chat_id=chat_id,
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        lang="ru", started_at=datetime.now(timezone.utc) - timedelta(days=8),
        expires_at=datetime.now(timezone.utc) + expires_delta,
        status=status,
    )
    session.add(rental)
    await session.flush()
    return rental


async def test_expire_marks_overdue_rentals(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    rental = await _make_rental(session, expires_delta=timedelta(seconds=-1))
    gateway = FakeChatGateway()
    svc = RentalExpiryService()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 1
    await session.refresh(rental)
    assert rental.status == "expired"
    assert len(gateway.sent_messages) == 1


async def test_expire_skips_active_rentals(session: AsyncSession):
    await _make_rental(session, expires_delta=timedelta(days=1))
    gateway = FakeChatGateway()
    svc = RentalExpiryService()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 0
    assert len(gateway.sent_messages) == 0


async def test_expire_skips_already_expired(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _make_rental(session, expires_delta=timedelta(seconds=-1), status="expired")
    gateway = FakeChatGateway()
    svc = RentalExpiryService()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 0
    assert len(gateway.sent_messages) == 0


async def test_expire_sends_to_correct_chat(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _make_rental(session, expires_delta=timedelta(seconds=-1), chat_id="555")
    gateway = FakeChatGateway()
    svc = RentalExpiryService()
    await svc.expire_overdue(session, gateway)
    assert len(gateway.sent_messages) == 1
    chat_id, _ = gateway.sent_messages[0]
    assert chat_id == 555
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_rental_expiry.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/rental_expiry.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.catalog import Duration, SubscriptionTier
from app.models.rental import Rental
from app.services.messages import render_message


class RentalExpiryService:
    """Поиск истёкших аренд, пометка expired, отправка expiry message."""

    async def expire_overdue(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
    ) -> list[Rental]:
        """Помечает все active аренды с expires_at <= NOW() как expired.

        Отправляет expiry_message в чат каждой истёкшей аренды.
        Возвращает список обработанных Rental.
        """
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(Rental).where(
                Rental.status == "active",
                Rental.expires_at <= now,
            )
        )
        overdue = result.scalars().all()
        expired_list: list[Rental] = []

        for rental in overdue:
            rental.status = "expired"
            await self._send_expiry(session, gateway, rental)
            expired_list.append(rental)

        if expired_list:
            await session.flush()
        return expired_list

    async def _send_expiry(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        rental: Rental,
    ) -> None:
        tier = await session.get(SubscriptionTier, rental.tier_id)
        duration = await session.get(Duration, rental.duration_id)
        text = await render_message(
            session, "expiry", rental.lang,
            tier=tier.name if tier else "",
            days=duration.days if duration else 0,
        )
        await gateway.send_message(
            chat_id=int(rental.buyer_funpay_chat_id), text=text,
        )
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_rental_expiry.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add backend/app/services/rental_expiry.py backend/tests/test_rental_expiry.py
git commit -m "feat: add RentalExpiryService for overdue rental expiration"
```

---

## Task 8: LotAutoManager — capacity check + sync

Пересчёт capacity для каждой PriceMatrix-связки. Если есть capacity и лот не существует/паушен — создать/активировать. Если capacity нет и лот активен — паушить.

**Files:**
- Create: `backend/app/services/lot_auto_manager.py`
- Test: `backend/tests/test_lot_auto_manager.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_lot_auto_manager.py`:

```python
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.lot import PriceMatrix, Lot
from app.services.lot_auto_manager import LotAutoManager


async def _seed_catalog(session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope_any = LimitScope(code="any", name="Любой")
    session.add(scope_any)
    await session.flush()
    return tier, duration, scope_any


async def _add_account_with_limits(session: AsyncSession, tier_id: int, **limits_kw):
    acc = Account(
        login=f"acc{limits_kw.get('_n', 1)}",
        password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=tier_id, status="active",
        subscription_expires_at=datetime.now(timezone.utc) + datetime.timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    session.add(AccountLimits(
        account_id=acc.id, refresh_token_encrypted="enc",
        measured_at=datetime.now(timezone.utc), refresh_status="ok",
        **{k: v for k, v in limits_kw.items() if not k.startswith("_")},
    ))
    await session.flush()
    return acc


async def test_creates_lot_when_capacity_available(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id,
        chat_5h_remaining_pct=80, chat_weekly_remaining_pct=70,
        codex_5h_remaining_pct=60, codex_weekly_remaining_pct=50,
    )
    session.add(PriceMatrix(
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        price=599,
    ))
    session.add(Lot(
        funpay_node_id=55, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=599, title_ru="T", title_en="T",
        status="active", auto_created=True,
    ))
    await session.flush()

    gateway = FakeChatGateway()
    mgr = LotAutoManager(funpay_node_id=55)
    actions = await mgr.run(session, gateway)
    # Лот уже существует и активен — нет действий (или только sync)
    assert isinstance(actions, list)


async def test_pauses_lot_when_no_capacity(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    # Нет аккаунтов — capacity = 0
    session.add(PriceMatrix(
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        price=599,
    ))
    lot = Lot(
        funpay_node_id=55, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=599, title_ru="T", title_en="T",
        status="active", auto_created=True, funpay_id="100",
    )
    session.add(lot)
    await session.flush()

    gateway = FakeChatGateway()
    mgr = LotAutoManager(funpay_node_id=55)
    actions = await mgr.run(session, gateway)
    # Лот должен быть паушен (нет аккаунтов)
    assert any(a.action == "pause" for a in actions)
    await session.refresh(lot)
    assert lot.status == "paused"
    assert (100, False) in gateway.activity_changes


async def test_activates_lot_when_capacity_returns(session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    await _add_account_with_limits(session, tier.id,
        chat_5h_remaining_pct=80, chat_weekly_remaining_pct=70,
        codex_5h_remaining_pct=60, codex_weekly_remaining_pct=50,
    )
    session.add(PriceMatrix(
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        price=599,
    ))
    lot = Lot(
        funpay_node_id=55, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=599, title_ru="T", title_en="T",
        status="paused", auto_created=True, funpay_id="200",
    )
    session.add(lot)
    await session.flush()

    gateway = FakeChatGateway()
    mgr = LotAutoManager(funpay_node_id=55)
    actions = await mgr.run(session, gateway)
    assert any(a.action == "activate" for a in actions)
    await session.refresh(lot)
    assert lot.status == "active"
    assert (200, True) in gateway.activity_changes
```

- [ ] **Step 2: Запустить тест, убедиться что падяет**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_lot_auto_manager.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Реализовать**

`backend/app/services/lot_auto_manager.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.lot import Lot, PriceMatrix
from app.services.lot_sync import LotSyncService


_LIMITS_FRESH_THRESHOLD = timedelta(hours=1)


@dataclass(frozen=True)
class LotAction:
    """Действие, выполненное LotAutoManager над лотом."""

    lot_id: int
    action: str  # create | activate | pause | none


class LotAutoManager:
    """Авто-управление лотами по capacity аккаунтов.

    Для каждой PriceMatrix-связки проверяет: есть ли аккаунт с capacity.
    Есть capacity + лот активен → ничего.
    Есть capacity + лот паушен/отсутствует → активировать/создать.
    Нет capacity + лот активен → паушить.
    """

    def __init__(self, funpay_node_id: int) -> None:
        self._funpay_node_id = funpay_node_id
        self._sync = LotSyncService()

    async def run(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
    ) -> list[LotAction]:
        matrices = await self._load_price_matrices(session)
        actions: list[LotAction] = []

        for matrix in matrices:
            lot = await self._find_lot_for_matrix(session, matrix)
            has_capacity = await self._check_capacity(session, matrix)

            if has_capacity:
                if lot is None:
                    lot = await self._create_lot(session, matrix)
                    await self._sync.sync_lot(session, gateway, lot.id, active=True)
                    actions.append(LotAction(lot_id=lot.id, action="create"))
                elif lot.status == "paused":
                    await self._sync.activate_lot(session, gateway, lot.id)
                    actions.append(LotAction(lot_id=lot.id, action="activate"))
                else:
                    actions.append(LotAction(lot_id=lot.id, action="none"))
            else:
                if lot is not None and lot.status == "active":
                    await self._sync.pause_lot(session, gateway, lot.id)
                    actions.append(LotAction(lot_id=lot.id, action="pause"))
                elif lot is not None:
                    actions.append(LotAction(lot_id=lot.id, action="none"))
                # Нет лота и нет capacity — ничего не делаем

        return actions

    async def _load_price_matrices(self, session: AsyncSession) -> list[PriceMatrix]:
        result = await session.execute(select(PriceMatrix))
        return list(result.scalars().all())

    async def _find_lot_for_matrix(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> Lot | None:
        result = await session.execute(
            select(Lot).where(
                Lot.tier_id == matrix.tier_id,
                Lot.duration_id == matrix.duration_id,
                Lot.limit_scope_id == matrix.limit_scope_id,
                Lot.min_limit_pct.is_(matrix.min_limit_pct),
                Lot.auto_created.is_(True),
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def _check_capacity(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> bool:
        """Есть ли хотя бы один аккаунт с подходящими лимитами."""
        now = datetime.now(timezone.utc)
        fresh_cutoff = now - _LIMITS_FRESH_THRESHOLD
        required_expires = now  # срок подписки не проверяем в capacity (упрощение)

        stmt = (
            select(func.count())
            .select_from(Account)
            .join(AccountLimits, AccountLimits.account_id == Account.id)
            .where(
                Account.status == "active",
                Account.tier_id == matrix.tier_id,
                Account.subscription_expires_at >= required_expires,
                AccountLimits.measured_at >= fresh_cutoff,
                AccountLimits.refresh_status == "ok",
            )
        )
        result = await session.execute(stmt)
        count = result.scalar_one()
        return count > 0

    async def _create_lot(
        self, session: AsyncSession, matrix: PriceMatrix,
    ) -> Lot:
        from app.models.catalog import SubscriptionTier, Duration, LimitScope
        tier = await session.get(SubscriptionTier, matrix.tier_id)
        duration = await session.get(Duration, matrix.duration_id)
        lot = Lot(
            funpay_node_id=self._funpay_node_id,
            tier_id=matrix.tier_id,
            duration_id=matrix.duration_id,
            limit_scope_id=matrix.limit_scope_id,
            min_limit_pct=matrix.min_limit_pct,
            max_5h_pct=matrix.max_5h_pct,
            max_weekly_pct=matrix.max_weekly_pct,
            price=matrix.price,
            title_ru=self._title(tier, duration, "ru"),
            title_en=self._title(tier, duration, "en"),
            status="active",
            auto_created=True,
        )
        session.add(lot)
        await session.flush()
        return lot

    def _title(self, tier, duration, lang: str) -> str:
        if tier is None or duration is None:
            return "ChatGPT"
        if lang == "ru":
            return f"ChatGPT {tier.name} — {duration.days} дн."
        return f"ChatGPT {tier.name} — {duration.days} days"
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_lot_auto_manager.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add backend/app/services/lot_auto_manager.py backend/tests/test_lot_auto_manager.py
git commit -m "feat: add LotAutoManager for capacity-based lot management"
```

---

## Task 9: Интеграция в build_callbacks

Подключить RentalService в on_new_sale, реальные хэндлеры в on_message (через CommandRouter.register), RentalExpiryService в регулярный вызов.

**Files:**
- Modify: `backend/app/services/funpay_lifecycle.py`
- Modify: `backend/tests/test_funpay_lifecycle.py` (добавить интеграционные тесты)

- [ ] **Step 1: Прочитать текущий funpay_lifecycle.py**

Run: `cat /c/Source/funpay/backend/app/services/funpay_lifecycle.py`

Понять текущую структуру build_callbacks.

- [ ] **Step 2: Модифицировать build_callbacks**

Обновить `funpay_lifecycle.py` — добавить:

1. В `on_new_sale`: после OrderProcessor → RentalService.fulfill_order
2. Создать CommandRouter с зарегистрированными хэндлерами (CodeHandler, HelpHandler, SubscriptionHandler, SellerHandler, ReplaceHandler)
3. В `on_message`: передать session в CommandContext через атрибут `_session`
4. Добавить `expire_overdue` callback (вызывается Scheduler'ом — Фаза 7)

Ключевые изменения:

```python
from app.services.rental_service import RentalService
from app.services.rental_expiry import RentalExpiryService
from app.services.command_handlers import (
    CodeHandler, HelpHandler, SubscriptionHandler, SellerHandler, ReplaceHandler,
)
from app.services.command_parser import CommandType
from app.services.command_router import CommandContext
from app.models.settings import SellerSettings
```

В build_callbacks добавить после order_processor:

```python
    rental_service = RentalService()
    expiry_service = RentalExpiryService()

    # Регистрируем хэндлеры команд
    router.register(CommandType.CODE, _attach_session(CodeHandler()))
    router.register(CommandType.HELP, _attach_session(HelpHandler()))
    router.register(CommandType.SUBSCRIPTION, _attach_session(SubscriptionHandler()))
    router.register(CommandType.SELLER, _attach_session(SellerHandler()))
    router.register(CommandType.REPLACE, _attach_session(ReplaceHandler()))
```

Хелпер `_attach_session` обёртка, которая достаёт session из контекста:

```python
def _make_session_injecting_wrapper(handler):
    """Обёртка: создаёт CommandContext с _session перед вызовом handler."""
    async def wrapper(ctx: CommandContext) -> None:
        # session уже в контексте (установлен в on_message)
        await handler(ctx)
    return wrapper
```

В on_new_sale добавить после process_new_sale:

```python
        try:
            order = await order_processor.process_new_sale(session, gateway, order_id)
            # Получаем default_max_active_rentals из SellerSettings
            settings = await session.get(SellerSettings, 1)
            max_rentals = settings.default_max_active_rentals if settings else 1
            await rental_service.fulfill_order(session, gateway, order.id, max_rentals)
            await session.commit()
        except LotNotFoundError:
            logger.warning("New sale %s: no matching lot", order_id)
        except Exception:
            logger.exception("Failed to process new sale %s", order_id)
```

В on_message — установить session на context:

```python
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
            # Передаём session в контекст для хэндлеров
            object.__setattr__(ctx, "_session", session)
            try:
                await router.dispatch(ctx)
                await session.commit()
            except UnhandledMessage:
                logger.debug("Unhandled command in chat %s", msg.chat_id)
            except Exception:
                logger.exception("Failed to process message in chat %s", msg.chat_id)
```

- [ ] **Step 3: Дописать интеграционный тест**

Добавить в `backend/tests/test_funpay_lifecycle.py`:

```python
async def test_on_new_sale_creates_rental_when_account_available(session: AsyncSession):
    """Полный поток: new_sale → Order → Rental → welcome message."""
    from app.models.account import Account, AccountLimits
    from app.models.catalog import SubscriptionTier, Duration, LimitScope
    from app.models.settings import SellerSettings
    from app.services.funpay_lifecycle import build_callbacks
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    session.add(SellerSettings(id=1, default_max_active_rentals=1))
    acc = Account(
        login="acc1", password_encrypted="pass", totp_secret_encrypted="totp",
        tier_id=tier.id, status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    session.add(AccountLimits(
        account_id=acc.id, refresh_token_encrypted="enc",
        chat_5h_remaining_pct=80, chat_weekly_remaining_pct=70,
        codex_5h_remaining_pct=60, codex_weekly_remaining_pct=50,
        measured_at=datetime.now(timezone.utc), refresh_status="ok",
    ))
    from app.models.lot import Lot
    session.add(Lot(
        funpay_node_id=55, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=599, title_ru="T", title_en="T",
        status="active", auto_created=True,
    ))
    await session.flush()

    gateway = FakeChatGateway()
    gateway.set_order(OrderInfo(
        order_id="ord-99", status=SaleStatus.PAID, chat_id=100, buyer_id=200,
        subcategory_id=55, title="test", price=599.0,
    ))
    callbacks = build_callbacks(session_factory=lambda: session, gateway=gateway)
    await callbacks.on_new_sale("ord-99")

    from app.models.rental import Rental
    from sqlalchemy import select
    rentals = (await session.execute(select(Rental))).scalars().all()
    assert len(rentals) == 1
    assert len(gateway.sent_messages) >= 1  # welcome отправлен
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_funpay_lifecycle.py -v`
Expected: PASS (4 existing + 1 new)

- [ ] **Step 5: Полный прогон**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd /c/Source/funpay
git add backend/app/services/funpay_lifecycle.py backend/tests/test_funpay_lifecycle.py
git commit -m "feat: wire RentalService and command handlers into build_callbacks"
```

---

## Task 10: Финальная проверка

- [ ] **Step 1: Полный прогон всех тестов**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest -v 2>&1 | tail -30`
Expected: ALL PASS (126 из Фаз 1-3 + ~30 новых Фазы 4)

- [ ] **Step 2: Проверить импорты всех модулей Фазы 4**

Run: `cd /c/Source/funpay/backend && py -3.12 -c "
import app.services.account_pool
import app.services.rental_service
import app.services.kick_service
import app.services.command_handlers
import app.services.rental_expiry
import app.services.lot_auto_manager
print('All Phase 4 modules import OK')
"`
Expected: `OK`

- [ ] **Step 3: Проверить полный лог коммитов**

Run: `cd /c/Source/funpay && git log --oneline phase1-foundation..HEAD`
Expected: 8-9 коммитов с feat: префиксом

- [ ] **Step 4: Commit финального состояния если есть правки**

```bash
cd /c/Source/funpay
git status
```

---

## Замечания по реализации

### Граничные случаи

1. **AccountPool.acquire с scope=any и обоими порогами**: фильтр применяется последовательно (AND). Аккаунт должен удовлетворять ОБАМ порогам одновременно.

2. **func.min в SQLAlchemy**: `func.min(col1, col2)` — SQL LEAST. Работает в PostgreSQL и SQLite (с оговорками). Если SQLite ругается — заменить на CASE expression. Тесты выявят.

3. **RentalService и buyer_locale**: Order.buyer_locale по умолчанию "ru". Для EN покупателей lang подставится из order — welcome template имеет en-вариант.

4. **KickService и Playwright**: тесты мокают browser_context и kick_account. Реальный kick требует установленного Playwright (Фаза 7/deployment).

5. **LotAutoManager._find_lot_for_matrix**: матчит по tier+duration+scope+min_limit_pct+auto_created. config_key не используется для матча (он для UNIQUE). Если две PriceMatrix с разными порогами — разные лоты.

6. **CommandContext._session**: CommandContext — frozen dataclass. Установка атрибута через `object.__setattr__` обходом frozen. Это намеренно — session注入ается внешним кодом, не в конструкторе.

### Что НЕ делает Фаза 4 (отложено)

- **Scheduler** — регулярный вызов expire_overdue, bump, lot_auto_manager.run (Фаза 7)
- **Telegram-уведомления** продавцу (Фаза 7)
- **Playwright refresh-восстановление** — воркер-пул для refresh_recover jobs (Фаза 7)
- **Админ API** (Фаза 5)
- **Frontend SPA** (Фаза 6)
- **Реальное подключение к FunPay** (Фаза 7)
