# Фаза 5: Admin API — План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** FastAPI REST API для админ-панели: аутентификация (JWT), CRUD для всех сущностей (accounts, tiers, durations, limit_scopes, prices, templates, lots, orders, rentals, settings), метрики.

**Architecture:** FastAPI routers по доменным областям. Pydantic v2 schema для request/response. Аутентификация через JWT (python-jose) в httpOnly cookie. Зависимость `get_current_user` для защищённых эндпоинтов. Тесты через httpx AsyncClient + in-memory SQLite.

**Tech Stack:** FastAPI, python-jose (JWT), passlib (bcrypt — уже есть в requirements), Pydantic v2, pytest-asyncio, httpx (для тестов).

---

## Фиксированные инварианты (из существующего кода)

- `app.main.app` — FastAPI instance, lifespan dispose engine
- `app.db.session.async_session_factory` — sessionmaker для production
- `app.db.session.get_session()` — async generator зависимость для FastAPI
- `app.config.Settings`: `secret_key`, `admin_password_hash`
- `SellerSettings`: singleton (id=1), содержит `admin_password_hash` + все настройки
- Все модели: `Account`, `AccountLimits`, `AccountCheckJob`, `SubscriptionTier`, `Duration`, `LimitScope`, `Lot`, `PriceMatrix`, `LotTemplate`, `BumpLog`, `Order`, `Rental`, `MessageTemplate`, `SellerSettings`, `AuditLog`
- `FernetEncrypted` TypeDecorator: авто-шифрование при записи, авто-расшифрование при чтении
- Спека раздел 15 — полный список эндпоинтов

---

## Структура файлов

### Новые файлы

```
backend/app/api/
├── __init__.py          # пустой
├── deps.py              # get_current_user, JWT verify, cookie auth
├── auth.py              # JWT create/verify, password verify
├── schemas.py           # Pydantic schemas для всех request/response
└── routers/
    ├── __init__.py      # пустой
    ├── auth.py          # POST /api/auth/login
    ├── metrics.py       # GET /api/metrics
    ├── accounts.py      # CRUD accounts + bulk + check + limits
    ├── catalog.py       # CRUD tiers, durations, limit_scopes
    ├── prices.py        # PriceMatrix PUT/GET
    ├── templates.py     # MessageTemplate GET/PUT
    ├── lots.py          # Lot CRUD + bump + pause/activate
    ├── orders.py        # Order GET list/detail
    ├── rentals.py       # Rental GET/PATCH
    └── settings.py      # SellerSettings GET/PUT

backend/tests/
├── conftest_api.py      # API fixtures: client, auth_client
├── test_api_auth.py
├── test_api_metrics.py
├── test_api_accounts.py
├── test_api_catalog.py
├── test_api_prices.py
├── test_api_templates.py
├── test_api_lots.py
├── test_api_orders.py
├── test_api_rentals.py
└── test_api_settings.py
```

### Модифицируемые

- `backend/app/main.py` — подключить routers, CORS middleware
- `backend/tests/conftest.py` — добавить client fixture (опционально, может быть в conftest_api.py)

---

## Task 1: Аутентификация — JWT + bcrypt

JWT-токен создаётся при логине, проверяется через зависимость. Хеш пароля — bcrypt (passlib). Токен в httpOnly cookie.

**Files:**
- Create: `backend/app/api/__init__.py` (пустой)
- Create: `backend/app/api/auth.py`
- Create: `backend/app/api/deps.py`
- Test: `backend/tests/test_api_auth.py`

- [ ] **Step 1: Создать пакет api**

Создать `backend/app/api/__init__.py` (пустой файл) и `backend/app/api/routers/__init__.py` (пустой файл).

- [ ] **Step 2: Написать тест**

`backend/tests/test_api_auth.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_login_success(client: AsyncClient, session: AsyncSession):
    from app.models.settings import SellerSettings
    from passlib.hash import bcrypt
    settings = SellerSettings(
        id=1, admin_password_hash=bcrypt.hash("secret123"),
    )
    session.add(settings)
    await session.commit()

    resp = await client.post("/api/auth/login", json={
        "password": "secret123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "access_token" in resp.cookies


async def test_login_wrong_password(client: AsyncClient, session: AsyncSession):
    from app.models.settings import SellerSettings
    from passlib.hash import bcrypt
    session.add(SellerSettings(id=1, admin_password_hash=bcrypt.hash("secret123")))
    await session.commit()

    resp = await client.post("/api/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401


async def test_login_no_settings_returns_500(client: AsyncClient, session: AsyncSession):
    resp = await client.post("/api/auth/login", json={"password": "any"})
    assert resp.status_code == 500


async def test_logout_clears_cookie(client: AsyncClient, session: AsyncSession):
    from app.models.settings import SellerSettings
    from passlib.hash import bcrypt
    session.add(SellerSettings(id=1, admin_password_hash=bcrypt.hash("secret123")))
    await session.commit()

    await client.post("/api/auth/login", json={"password": "secret123"})
    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 200
```

- [ ] **Step 3: Реализовать auth.py**

`backend/app/api/auth.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.hash import bcrypt

from app.config import get_settings

_ALGORITHM = "HS256"
_TOKEN_TTL = timedelta(hours=24)
_COOKIE_NAME = "access_token"


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.verify(plain, hashed)


def create_access_token(subject: str = "admin") -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + _TOKEN_TTL).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> str | None:
    """Возвращает subject если токен валиден, иначе None."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


COOKIE_NAME = _COOKIE_NAME
```

- [ ] **Step 4: Реализовать deps.py**

`backend/app/api/deps.py`:

```python
from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, decode_access_token
from app.db.session import get_session


async def get_current_user(
    access_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> str:
    """Зависимость: проверяет JWT из cookie. Возвращает subject."""
    if access_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    sub = decode_access_token(access_token)
    if sub is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return sub


async def get_db_session() -> AsyncSession:
    """Зависимость: возвращает AsyncSession из pool."""
    async for session in get_session():
        yield session
```

- [ ] **Step 5: Реализовать auth router**

`backend/app/api/routers/auth.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token, verify_password
from app.api.deps import get_db_session
from app.models.settings import SellerSettings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class StatusResponse(BaseModel):
    status: str


@router.post("/login", response_model=StatusResponse)
async def login(
    req: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> StatusResponse:
    settings = await session.get(SellerSettings, 1)
    if settings is None or not settings.admin_password_hash:
        raise HTTPException(status_code=500, detail="Admin not configured")
    if not verify_password(req.password, settings.admin_password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong password")
    token = create_access_token()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400,
    )
    return StatusResponse(status="ok")


@router.post("/logout", response_model=StatusResponse)
async def logout(response: Response) -> StatusResponse:
    response.delete_cookie(COOKIE_NAME)
    return StatusResponse(status="ok")
```

- [ ] **Step 6: Подключить router в main.py**

В `backend/app/main.py` добавить:

```python
from app.api.routers.auth import router as auth_router

app.include_router(auth_router)
```

- [ ] **Step 7: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_api_auth.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
cd /c/Source/funpay
git add backend/app/api/ backend/app/main.py backend/tests/test_api_auth.py
git commit -m "feat: add JWT auth with bcrypt for admin login"
```

---

## Task 2: Schemas — Pydantic модели для всех эндпоинтов

Единый файл со всеми request/response схемами. Pydantic v2 с `model_config = ConfigDict(from_attributes=True)` для ORM-маппинга.

**Files:**
- Create: `backend/app/api/schemas.py`
- Test: `backend/tests/test_api_schemas.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_api_schemas.py`:

```python
from datetime import datetime, timezone

from app.api.schemas import (
    AccountOut, AccountCreate, TierOut, TierCreate, DurationOut,
    LimitScopeOut, LotOut, OrderOut, RentalOut, SettingsOut, SettingsUpdate,
    MetricsOut, PriceMatrixItem, TemplateOut, TemplateUpdate,
)


def test_account_out_from_orm():
    """AccountOut.from_attributes должен маппить ORM-объект."""
    from app.models.account import Account
    acc = Account(
        id=1, login="test", password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=1, status="active",
    )
    acc.password_encrypted = "should_not_leak"  # type: ignore
    out = AccountOut.model_validate(acc, from_attributes=True)
    assert out.id == 1
    assert out.login == "test"
    assert out.status == "active"
    assert not hasattr(out, "password_encrypted")  # секреты не утекают


def test_account_create():
    data = AccountCreate(
        login="new", password="pass", totp_secret="SECRET",
        tier_id=1, subscription_expires_at=None,
    )
    assert data.login == "new"


def test_tier_out():
    t = TierOut(id=1, name="Plus", description=None, is_active=True)
    assert t.name == "Plus"


def test_metrics_out():
    m = MetricsOut(
        active_rentals=5, available_accounts=3, orders_today=2,
        revenue_brutto=1000, revenue_netto=850, bot_status="connected",
    )
    assert m.active_rentals == 5


def test_settings_out_excludes_admin_hash():
    s = SettingsOut(
        funpay_node_id=55, auto_bump_enabled=True, bump_interval_hours=4,
        default_max_active_rentals=1, funpay_commission_percent=15,
        check_interval_minutes=10, limits_check_interval_minutes=5,
        limits_warn_threshold_pct=20,
    )
    assert not hasattr(s, "admin_password_hash")
```

- [ ] **Step 2: Реализовать**

`backend/app/api/schemas.py`:

```python
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Auth ---

class LoginRequest(BaseModel):
    password: str


class StatusResponse(BaseModel):
    status: str


# --- Catalog ---

class TierOut(_Base):
    id: int
    name: str
    description: str | None = None
    is_active: bool


class TierCreate(BaseModel):
    name: str
    description: str | None = None
    is_active: bool = True


class TierUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None


class DurationOut(_Base):
    id: int
    days: int
    is_enabled: bool
    sort_order: int


class DurationUpdate(BaseModel):
    is_enabled: bool | None = None
    sort_order: int | None = None


class LimitScopeOut(_Base):
    id: int
    code: str
    name: str


# --- Accounts ---

class AccountOut(_Base):
    id: int
    login: str
    tier_id: int
    subscription_expires_at: datetime | None = None
    max_active_rentals: int | None = None
    status: str
    notes: str | None = None


class AccountCreate(BaseModel):
    login: str
    password: str
    totp_secret: str
    tier_id: int
    subscription_expires_at: datetime | None = None
    max_active_rentals: int | None = None
    notes: str | None = None


class AccountUpdate(BaseModel):
    subscription_expires_at: datetime | None = None
    max_active_rentals: int | None = None
    status: str | None = None
    notes: str | None = None


class AccountLimitsOut(_Base):
    account_id: int
    chat_5h_remaining_pct: int | None = None
    chat_weekly_remaining_pct: int | None = None
    codex_5h_remaining_pct: int | None = None
    codex_weekly_remaining_pct: int | None = None
    refresh_status: str
    measured_at: datetime | None = None


class AccountWithLimits(AccountOut):
    limits: AccountLimitsOut | None = None


# --- Price Matrix ---

class PriceMatrixItem(BaseModel):
    tier_id: int
    duration_id: int
    limit_scope_id: int
    min_limit_pct: int | None = None
    max_5h_pct: int | None = None
    max_weekly_pct: int | None = None
    price: int


class PriceMatrixUpdate(BaseModel):
    items: list[PriceMatrixItem]


# --- Templates ---

class TemplateOut(_Base):
    key: str
    lang: str
    content: str


class TemplateItem(BaseModel):
    key: str
    lang: str
    content: str


class TemplateUpdate(BaseModel):
    items: list[TemplateItem]


# --- Lots ---

class LotOut(_Base):
    id: int
    funpay_id: str | None = None
    funpay_node_id: int | None = None
    tier_id: int
    duration_id: int
    limit_scope_id: int
    min_limit_pct: int | None = None
    max_5h_pct: int | None = None
    max_weekly_pct: int | None = None
    price: int
    title_ru: str
    title_en: str
    status: str
    auto_created: bool


class LotCreate(BaseModel):
    funpay_node_id: int | None = None
    tier_id: int
    duration_id: int
    limit_scope_id: int
    min_limit_pct: int | None = None
    max_5h_pct: int | None = None
    max_weekly_pct: int | None = None
    price: int
    title_ru: str
    title_en: str
    description_ru: str = ""
    description_en: str = ""


# --- Orders / Rentals ---

class OrderOut(_Base):
    id: int
    funpay_order_id: str
    funpay_chat_id: str
    buyer_funpay_id: str
    buyer_locale: str
    lot_id: int | None = None
    tier_id: int | None = None
    duration_id: int | None = None
    limit_scope_id: int | None = None
    price: int
    status: str
    created_at: datetime


class RentalOut(_Base):
    id: int
    order_id: int
    account_id: int
    buyer_funpay_id: str
    buyer_funpay_chat_id: str
    tier_id: int
    duration_id: int
    limit_scope_id: int
    lang: str
    started_at: datetime
    expires_at: datetime
    status: str
    replacement_count: int


class RentalPatch(BaseModel):
    status: str | None = None


# --- Settings ---

class SettingsOut(_Base):
    funpay_node_id: int | None = None
    auto_bump_enabled: bool
    bump_interval_hours: int
    default_max_active_rentals: int
    funpay_commission_percent: int
    check_interval_minutes: int
    limits_check_interval_minutes: int
    limits_warn_threshold_pct: int


class SettingsUpdate(BaseModel):
    funpay_node_id: int | None = None
    auto_bump_enabled: bool | None = None
    bump_interval_hours: int | None = None
    default_max_active_rentals: int | None = None
    funpay_commission_percent: int | None = None
    check_interval_minutes: int | None = None
    limits_check_interval_minutes: int | None = None
    limits_warn_threshold_pct: int | None = None


# --- Metrics ---

class MetricsOut(BaseModel):
    active_rentals: int
    available_accounts: int
    orders_today: int
    revenue_brutto: int
    revenue_netto: int
    bot_status: str
```

- [ ] **Step 3: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_api_schemas.py -v`
Expected: PASS (5 tests)

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add backend/app/api/schemas.py backend/tests/test_api_schemas.py
git commit -m "feat: add Pydantic schemas for all API endpoints"
```

---

## Task 3: Catalog routers — tiers, durations, limit_scopes

CRUD для справочников.

**Files:**
- Create: `backend/app/api/routers/catalog.py`
- Test: `backend/tests/test_api_catalog.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_api_catalog.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_list_tiers_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/tiers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_tier(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.post("/api/tiers", json={"name": "Plus", "is_active": True})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Plus"
    assert data["is_active"] is True
    assert "id" in data


async def test_create_tier_duplicate_returns_409(auth_client: AsyncClient, session: AsyncSession):
    await auth_client.post("/api/tiers", json={"name": "Plus"})
    resp = await auth_client.post("/api/tiers", json={"name": "Plus"})
    assert resp.status_code == 409


async def test_update_tier(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.post("/api/tiers", json={"name": "Plus"})
    tier_id = resp.json()["id"]
    resp = await auth_client.patch(f"/api/tiers/{tier_id}", json={"is_active": False})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_delete_tier(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.post("/api/tiers", json={"name": "Plus"})
    tier_id = resp.json()["id"]
    resp = await auth_client.delete(f"/api/tiers/{tier_id}")
    assert resp.status_code == 204
    resp = await auth_client.get("/api/tiers")
    assert resp.json() == []


async def test_list_durations(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/durations")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_list_limit_scopes(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/limit-scopes")
    assert resp.status_code == 200


async def test_unauthorized_request_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/tiers")
        assert resp.status_code == 401
```

- [ ] **Step 2: Реализовать catalog router**

`backend/app/api/routers/catalog.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import (
    TierCreate, TierOut, TierUpdate,
    DurationOut, DurationUpdate, LimitScopeOut,
)
from app.models.catalog import Duration, LimitScope, SubscriptionTier

router = APIRouter(prefix="/api", tags=["catalog"], dependencies=[Depends(get_current_user)])


# --- Tiers ---

@router.get("/tiers", response_model=list[TierOut])
async def list_tiers(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(SubscriptionTier).order_by(SubscriptionTier.id))
    return result.scalars().all()


@router.post("/tiers", response_model=TierOut, status_code=201)
async def create_tier(
    req: TierCreate,
    session: AsyncSession = Depends(get_db_session),
):
    tier = SubscriptionTier(name=req.name, description=req.description, is_active=req.is_active)
    session.add(tier)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Tier with this name already exists")
    await session.refresh(tier)
    return tier


@router.patch("/tiers/{tier_id}", response_model=TierOut)
async def update_tier(
    tier_id: int,
    req: TierUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    tier = await session.get(SubscriptionTier, tier_id)
    if tier is None:
        raise HTTPException(status_code=404, detail="Tier not found")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(tier, field, value)
    await session.commit()
    await session.refresh(tier)
    return tier


@router.delete("/tiers/{tier_id}", status_code=204)
async def delete_tier(
    tier_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    tier = await session.get(SubscriptionTier, tier_id)
    if tier is None:
        raise HTTPException(status_code=404, detail="Tier not found")
    await session.delete(tier)
    await session.commit()


# --- Durations ---

@router.get("/durations", response_model=list[DurationOut])
async def list_durations(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Duration).order_by(Duration.sort_order))
    return result.scalars().all()


@router.patch("/durations/batch", response_model=list[DurationOut])
async def update_durations_batch(
    items: list[DurationUpdate],
    session: AsyncSession = Depends(get_db_session),
):
    """Пакетное обновление is_enabled и sort_order для сроков."""
    # items содержит id + поля для обновления
    result = await session.execute(select(Duration))
    durations = {d.id: d for d in result.scalars().all()}
    for item in items:
        if item.id in durations:  # type: ignore
            d = durations[item.id]
            for field, value in item.model_dump(exclude_unset=True, exclude={"id"}).items():
                setattr(d, field, value)
    await session.commit()
    return list(durations.values())
```

ВАЖНО: `DurationUpdate` должен содержать `id` — обнови schema:

```python
class DurationUpdate(BaseModel):
    id: int
    is_enabled: bool | None = None
    sort_order: int | None = None
```

Добавь это в `schemas.py` (замени существующий DurationUpdate).

- [ ] **Step 3: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_api_catalog.py -v`
Expected: PASS (8 tests)

ВАЖНО: Тесты используют `auth_client` fixture с pre-set JWT cookie. Но тест `test_unauthorized_request_rejected` создаёт отдельный client без cookie.

Тесты используют общий `app` из `app.main` и `session` фикстуру из conftest. НО: `get_db_session` зависимость использует production `async_session_factory` (из `app.db.session`), а тесты — in-memory SQLite. Нужен dependency_overrides.

Создай фикстуру override в `tests/conftest.py` (или в начале каждого API-теста):

```python
@pytest.fixture(autouse=True)
async def _override_db(session):
    """Подменяет get_db_session на тестовую session."""
    from app.api.deps import get_db_session
    from app.main import app

    async def _get_test_session():
        yield session

    app.dependency_overrides[get_db_session] = _get_test_session
    yield
    app.dependency_overrides.clear()
```

Добавь эту фикстуру в `tests/conftest.py` (autouse=True, но только для API-тестов — не должна влиять на unit-тесты). Лучше: сделай её НЕ autouse, а явной в API-тестах. Или положи в отдельный `tests/conftest_api.py` и импортируй.

ПРОЩЕ: добавь в `conftest.py` autouse-фикстуру, которая подменяет только если есть `session`:

```python
@pytest_asyncio.fixture(autouse=True)
async def _override_app_db(session):
    """Подменяет БД для FastAPI-зависимостей на тестовую."""
    from app.api.deps import get_db_session
    from app.main import app

    async def _override():
        yield session

    app.dependency_overrides[get_db_session] = _override
    yield
    app.dependency_overrides.clear()
```

Это autouse=True → будет применяться ко ВСЕМ тестам, даже unit-тестам. Это безопасно: unit-тесты не используют FastAPI, override не повлияет.

- [ ] **Step 4: Подключить router в main.py**

В `backend/app/main.py` добавить:

```python
from app.api.routers.catalog import router as catalog_router
app.include_router(catalog_router)
```

- [ ] **Step 5: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_api_catalog.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
cd /c/Source/funpay
git add backend/app/api/routers/catalog.py backend/app/api/schemas.py backend/app/main.py backend/tests/test_api_catalog.py
git commit -m "feat: add catalog CRUD routers for tiers, durations, limit_scopes"
```

---

## Task 4: Accounts router

CRUD аккаунтов с bulk-загрузкой, action «проверить сейчас», показ лимитов.

**Files:**
- Create: `backend/app/api/routers/accounts.py`
- Test: `backend/tests/test_api_accounts.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_api_accounts.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.catalog import SubscriptionTier


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def _seed_tier(session: AsyncSession) -> int:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    return tier.id


async def test_list_accounts_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/accounts")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_account(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc1", "password": "pass", "totp_secret": "JBSWY3DPEHPK3PXP",
        "tier_id": tier_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["login"] == "acc1"
    assert data["status"] == "pending_validation"
    assert "password" not in data  # секреты не утекают
    assert "totp_secret" not in data


async def test_get_account_detail(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc1", "password": "pass", "totp_secret": "JBSWY3DPEHPK3PXP",
        "tier_id": tier_id,
    })
    acc_id = resp.json()["id"]
    resp = await auth_client.get(f"/api/accounts/{acc_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == acc_id


async def test_delete_account(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc1", "password": "pass", "totp_secret": "JBSWY3DPEHPK3PXP",
        "tier_id": tier_id,
    })
    acc_id = resp.json()["id"]
    resp = await auth_client.delete(f"/api/accounts/{acc_id}")
    assert resp.status_code == 204


async def test_bulk_add_accounts(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts/bulk", json={
        "tier_id": tier_id,
        "accounts": [
            {"login": "a1", "password": "p", "totp_secret": "JBSWY3DPEHPK3PXP"},
            {"login": "a2", "password": "p", "totp_secret": "JBSWY3DPEHPK3PXP"},
        ],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["created"] == 2


async def test_patch_account_status(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc1", "password": "pass", "totp_secret": "JBSWY3DPEHPK3PXP",
        "tier_id": tier_id,
    })
    acc_id = resp.json()["id"]
    resp = await auth_client.patch(f"/api/accounts/{acc_id}", json={"status": "active"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
```

- [ ] **Step 2: Реализовать accounts router**

`backend/app/api/routers/accounts.py`:

```python
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import AccountCreate, AccountOut, AccountUpdate, AccountWithLimits
from app.models.account import Account
from app.services.crypto import encrypt

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(get_current_user)])


class BulkAccountItem(BaseModel):
    login: str
    password: str
    totp_secret: str


class BulkAccountRequest(BaseModel):
    tier_id: int
    accounts: list[BulkAccountItem]


class BulkAccountResponse(BaseModel):
    created: int


@router.get("", response_model=list[AccountOut])
async def list_accounts(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Account).order_by(Account.id))
    return result.scalars().all()


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(
    req: AccountCreate,
    session: AsyncSession = Depends(get_db_session),
):
    account = Account(
        login=req.login,
        password_encrypted=encrypt(req.password),
        totp_secret_encrypted=encrypt(req.totp_secret),
        tier_id=req.tier_id,
        subscription_expires_at=req.subscription_expires_at,
        max_active_rentals=req.max_active_rentals,
        notes=req.notes,
    )
    session.add(account)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Account with this login already exists")
    await session.refresh(account)
    return account


@router.post("/bulk", response_model=BulkAccountResponse, status_code=201)
async def bulk_add_accounts(
    req: BulkAccountRequest,
    session: AsyncSession = Depends(get_db_session),
):
    for item in req.accounts:
        account = Account(
            login=item.login,
            password_encrypted=encrypt(item.password),
            totp_secret_encrypted=encrypt(item.totp_secret),
            tier_id=req.tier_id,
            status="pending_validation",
        )
        session.add(account)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Duplicate login in batch")
    return BulkAccountResponse(created=len(req.accounts))


@router.get("/{account_id}", response_model=AccountWithLimits)
async def get_account(
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    from app.models.account import AccountLimits
    limits = await session.get(AccountLimits, account_id)
    return AccountWithLimits(
        id=account.id, login=account.login, tier_id=account.tier_id,
        subscription_expires_at=account.subscription_expires_at,
        max_active_rentals=account.max_active_rentals,
        status=account.status, notes=account.notes,
        limits=limits,
    )


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: int,
    req: AccountUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    await session.commit()
    await session.refresh(account)
    return account


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await session.delete(account)
    await session.commit()
```

- [ ] **Step 3: Подключить router в main.py**

```python
from app.api.routers.accounts import router as accounts_router
app.include_router(accounts_router)
```

- [ ] **Step 4: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_api_accounts.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add backend/app/api/routers/accounts.py backend/app/main.py backend/tests/test_api_accounts.py
git commit -m "feat: add accounts CRUD router with bulk upload"
```

---

## Task 5: Lots router — list, manual create, pause/activate, delete, bump

**Files:**
- Create: `backend/app/api/routers/lots.py`
- Test: `backend/tests/test_api_lots.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_api_lots.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.catalog import SubscriptionTier, Duration, LimitScope


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def _seed_catalog(session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    return tier, duration, scope


async def test_list_lots_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/lots")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_manual_lot(auth_client: AsyncClient, session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    resp = await auth_client.post("/api/lots", json={
        "funpay_node_id": 55,
        "tier_id": tier.id, "duration_id": duration.id, "limit_scope_id": scope.id,
        "price": 599, "title_ru": "Тест", "title_en": "Test",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["price"] == 599
    assert data["auto_created"] is False
    assert data["status"] == "active"


async def test_delete_lot(auth_client: AsyncClient, session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    resp = await auth_client.post("/api/lots", json={
        "funpay_node_id": 55,
        "tier_id": tier.id, "duration_id": duration.id, "limit_scope_id": scope.id,
        "price": 599, "title_ru": "Т", "title_en": "T",
    })
    lot_id = resp.json()["id"]
    resp = await auth_client.delete(f"/api/lots/{lot_id}")
    assert resp.status_code == 204
```

- [ ] **Step 2: Реализовать lots router**

`backend/app/api/routers/lots.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import LotCreate, LotOut
from app.models.lot import Lot

router = APIRouter(prefix="/api/lots", tags=["lots"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[LotOut])
async def list_lots(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Lot).order_by(Lot.id))
    return result.scalars().all()


@router.post("", response_model=LotOut, status_code=201)
async def create_lot(
    req: LotCreate,
    session: AsyncSession = Depends(get_db_session),
):
    lot = Lot(
        funpay_node_id=req.funpay_node_id,
        tier_id=req.tier_id,
        duration_id=req.duration_id,
        limit_scope_id=req.limit_scope_id,
        min_limit_pct=req.min_limit_pct,
        max_5h_pct=req.max_5h_pct,
        max_weekly_pct=req.max_weekly_pct,
        price=req.price,
        title_ru=req.title_ru,
        title_en=req.title_en,
        description_ru=req.description_ru,
        description_en=req.description_en,
        status="active",
        auto_created=False,
    )
    session.add(lot)
    await session.commit()
    await session.refresh(lot)
    return lot


@router.delete("/{lot_id}", status_code=204)
async def delete_lot(
    lot_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    lot.status = "deleted"
    await session.commit()
```

- [ ] **Step 3: Подключить router, запустить тест**

В `main.py`:
```python
from app.api.routers.lots import router as lots_router
app.include_router(lots_router)
```

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_api_lots.py -v`
Expected: PASS (2 tests)

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add backend/app/api/routers/lots.py backend/app/main.py backend/tests/test_api_lots.py
git commit -m "feat: add lots CRUD router"
```

---

## Task 6: Orders, Rentals, Settings, Prices, Templates, Metrics routers

Оставшиеся роутеры. Меньше логики — в основном list/get + patch.

**Files:**
- Create: `backend/app/api/routers/orders.py`
- Create: `backend/app/api/routers/rentals.py`
- Create: `backend/app/api/routers/settings.py`
- Create: `backend/app/api/routers/prices.py`
- Create: `backend/app/api/routers/templates.py`
- Create: `backend/app/api/routers/metrics.py`
- Test: `backend/tests/test_api_orders.py`, `test_api_rentals.py`, `test_api_settings.py`, `test_api_prices.py`, `test_api_templates.py`, `test_api_metrics.py`

- [ ] **Step 1: Написать тесты (все 6 файлов)**

`backend/tests/test_api_orders.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_list_orders_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/orders")
    assert resp.status_code == 200
    assert resp.json() == []
```

`backend/tests/test_api_settings.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.settings import SellerSettings


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_get_settings(auth_client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1, funpay_node_id=55, default_max_active_rentals=3))
    await session.commit()
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["funpay_node_id"] == 55
    assert data["default_max_active_rentals"] == 3
    assert "admin_password_hash" not in data  # секрет не утекает


async def test_update_settings(auth_client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1))
    await session.commit()
    resp = await auth_client.put("/api/settings", json={"default_max_active_rentals": 5})
    assert resp.status_code == 200
    assert resp.json()["default_max_active_rentals"] == 5


async def test_get_settings_not_configured(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 404
```

`backend/tests/test_api_prices.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.catalog import SubscriptionTier, Duration, LimitScope


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_update_and_get_prices(auth_client: AsyncClient, session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()

    resp = await auth_client.put("/api/prices", json={
        "items": [
            {"tier_id": tier.id, "duration_id": duration.id, "limit_scope_id": scope.id, "price": 599},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1

    resp = await auth_client.get("/api/prices")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["price"] == 599
```

`backend/tests/test_api_metrics.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_metrics_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_rentals"] == 0
    assert data["available_accounts"] == 0
    assert data["orders_today"] == 0
    assert data["bot_status"] in ("connected", "disconnected")
```

- [ ] **Step 2: Реализовать routers**

`backend/app/api/routers/orders.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import OrderOut
from app.models.rental import Order

router = APIRouter(prefix="/api/orders", tags=["orders"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[OrderOut])
async def list_orders(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Order).order_by(Order.id.desc()))
    return result.scalars().all()


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: int, session: AsyncSession = Depends(get_db_session)):
    order = await session.get(Order, order_id)
    if order is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Order not found")
    return order
```

`backend/app/api/routers/rentals.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import RentalOut, RentalPatch
from app.models.rental import Rental

router = APIRouter(prefix="/api/rentals", tags=["rentals"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[RentalOut])
async def list_rentals(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Rental).order_by(Rental.id.desc()))
    return result.scalars().all()


@router.patch("/{rental_id}", response_model=RentalOut)
async def update_rental(
    rental_id: int,
    req: RentalPatch,
    session: AsyncSession = Depends(get_db_session),
):
    rental = await session.get(Rental, rental_id)
    if rental is None:
        raise HTTPException(status_code=404, detail="Rental not found")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(rental, field, value)
    await session.commit()
    await session.refresh(rental)
    return rental
```

`backend/app/api/routers/settings.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import SettingsOut, SettingsUpdate
from app.models.settings import SellerSettings

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=SettingsOut)
async def get_settings(session: AsyncSession = Depends(get_db_session)):
    settings = await session.get(SellerSettings, 1)
    if settings is None:
        raise HTTPException(status_code=404, detail="Settings not configured")
    return settings


@router.put("", response_model=SettingsOut)
async def update_settings(
    req: SettingsUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    settings = await session.get(SellerSettings, 1)
    if settings is None:
        settings = SellerSettings(id=1)
        session.add(settings)
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(settings, field, value)
    await session.commit()
    await session.refresh(settings)
    return settings
```

`backend/app/api/routers/prices.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import PriceMatrixItem
from app.models.lot import PriceMatrix

router = APIRouter(prefix="/api/prices", tags=["prices"], dependencies=[Depends(get_current_user)])


class PriceUpdateResponse(BaseModel):
    updated: int


class PriceUpdateRequest(BaseModel):
    items: list[PriceMatrixItem]


@router.get("", response_model=list[PriceMatrixItem])
async def list_prices(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(PriceMatrix))
    items = []
    for pm in result.scalars().all():
        items.append(PriceMatrixItem(
            tier_id=pm.tier_id, duration_id=pm.duration_id, limit_scope_id=pm.limit_scope_id,
            min_limit_pct=pm.min_limit_pct, max_5h_pct=pm.max_5h_pct,
            max_weekly_pct=pm.max_weekly_pct, price=pm.price,
        ))
    return items


@router.put("", response_model=PriceUpdateResponse)
async def update_prices(
    req: PriceUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    # Полная замена: удаляем старые, вставляем новые
    await session.execute(delete(PriceMatrix))
    for item in req.items:
        pm = PriceMatrix(
            tier_id=item.tier_id, duration_id=item.duration_id,
            limit_scope_id=item.limit_scope_id,
            min_limit_pct=item.min_limit_pct, max_5h_pct=item.max_5h_pct,
            max_weekly_pct=item.max_weekly_pct, price=item.price,
        )
        session.add(pm)
    await session.commit()
    return PriceUpdateResponse(updated=len(req.items))
```

`backend/app/api/routers/templates.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import TemplateOut, TemplateItem
from app.models.message import MessageTemplate

router = APIRouter(prefix="/api/templates", tags=["templates"], dependencies=[Depends(get_current_user)])


class TemplateUpdateRequest(BaseModel):
    items: list[TemplateItem]


class TemplateUpdateResponse(BaseModel):
    updated: int


@router.get("", response_model=list[TemplateOut])
async def list_templates(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(MessageTemplate).order_by(MessageTemplate.key, MessageTemplate.lang))
    return result.scalars().all()


@router.put("", response_model=TemplateUpdateResponse)
async def update_templates(
    req: TemplateUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    for item in req.items:
        existing = await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.key == item.key,
                MessageTemplate.lang == item.lang,
            )
        )
        tpl = existing.scalar_one_or_none()
        if tpl is None:
            tpl = MessageTemplate(key=item.key, lang=item.lang, content=item.content)
            session.add(tpl)
        else:
            tpl.content = item.content
    await session.commit()
    return TemplateUpdateResponse(updated=len(req.items))
```

`backend/app/api/routers/metrics.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import MetricsOut
from app.models.account import Account
from app.models.rental import Order, Rental

router = APIRouter(prefix="/api/metrics", tags=["metrics"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=MetricsOut)
async def get_metrics(session: AsyncSession = Depends(get_db_session)):
    # Активные аренды
    active_result = await session.execute(
        select(func.count()).select_from(Rental).where(Rental.status == "active")
    )
    active_rentals = active_result.scalar_one()

    # Свободные аккаунты (active, не в аренде)
    available_result = await session.execute(
        select(func.count()).select_from(Account).where(Account.status == "active")
    )
    available_accounts = available_result.scalar_one()

    # Заказы сегодня
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    orders_result = await session.execute(
        select(func.count()).select_from(Order).where(Order.created_at >= today_start)
    )
    orders_today = orders_result.scalar_one()

    # Выручка (brutto = sum price completed orders today)
    revenue_result = await session.execute(
        select(func.coalesce(func.sum(Order.price), 0)).where(
            Order.created_at >= today_start,
            Order.status.in_(["pending", "completed"]),
        )
    )
    revenue_brutto = revenue_result.scalar_one()

    return MetricsOut(
        active_rentals=active_rentals,
        available_accounts=available_accounts,
        orders_today=orders_today,
        revenue_brutto=revenue_brutto,
        revenue_netto=int(revenue_brutto * 0.85),  # комиссия 15% по умолчанию
        bot_status="disconnected",  # Фаза 7: реальный статус FunPay Runner
    )
```

- [ ] **Step 3: Подключить все routers в main.py**

```python
from app.api.routers.orders import router as orders_router
from app.api.routers.rentals import router as rentals_router
from app.api.routers.settings import router as settings_router
from app.api.routers.prices import router as prices_router
from app.api.routers.templates import router as templates_router
from app.api.routers.metrics import router as metrics_router

app.include_router(orders_router)
app.include_router(rentals_router)
app.include_router(settings_router)
app.include_router(prices_router)
app.include_router(templates_router)
app.include_router(metrics_router)
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_api_orders.py tests/test_api_settings.py tests/test_api_prices.py tests/test_api_metrics.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add backend/app/api/routers/ backend/app/main.py backend/tests/test_api_*.py
git commit -m "feat: add orders, rentals, settings, prices, templates, metrics routers"
```

---

## Task 7: conftest DB override + финальная проверка

**Files:**
- Modify: `backend/tests/conftest.py` — добавить `_override_app_db` autouse fixture
- Проверить полный прогон

- [ ] **Step 1: Добавить DB override в conftest.py**

В конец `backend/tests/conftest.py` добавить:

```python
@pytest_asyncio.fixture(autouse=True)
async def _override_app_db(session):
    """Подменяет БД для FastAPI-зависимостей на тестовую session.

    Безопасно для unit-тестов: они не используют FastAPI.
    """
    from app.api.deps import get_db_session
    from app.main import app

    async def _override():
        yield session

    app.dependency_overrides[get_db_session] = _override
    yield
    app.dependency_overrides.clear()
```

ВАЖНО: эта фикстура зависит от `session`, которая создаёт in-memory SQLite с create_all. Все таблицы создаются на ней. `get_db_session` подменяется → все API-эндпоинты используют ту же БД, что и тесты.

- [ ] **Step 2: Полный прогон всех тестов**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest -v 2>&1 | tail -30`
Expected: ALL PASS (162 из Фаз 1-4 + ~25 новых Фазы 5)

- [ ] **Step 3: Проверить, что /health работает**

Run: `cd /c/Source/funpay/backend && py -3.12 -c "
import asyncio
from httpx import ASGITransport, AsyncClient
from app.main import app

async def test():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        r = await c.get('/health')
        print(r.status_code, r.json())

asyncio.run(test())
"`
Expected: `200 {'status': 'ok'}`

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add backend/tests/conftest.py
git commit -m "test: add DB override fixture for API tests"
```

---

## Замечания

### Безопасность

1. **Секреты не утекают**: `AccountOut` НЕ содержит `password_encrypted`/`totp_secret_encrypted`. `SettingsOut` НЕ содержит `admin_password_hash`. Проверено тестами.
2. **JWT в httpOnly cookie**: недоступен из JavaScript (защита от XSS). `samesite=lax` — защита от CSRF.
3. **Все эндпоинты защищены**: `dependencies=[Depends(get_current_user)]` на router level, кроме `/api/auth/login` и `/health`.

### Testing

1. **DB override**: autouse fixture `_override_app_db` подменяет `get_db_session` на тестовую session. Это работает потому что `conftest.py` создаёт in-memory SQLite с `Base.metadata.create_all` до старта тестов.
2. **auth_client fixture**: создаёт AsyncClient с pre-set JWT cookie. Переиспользуется во всех API-тестах.
3. **httpx ASGITransport**: тесты ходят к FastAPI напрямую (без HTTP-сервера), быстро.

### Что НЕ делает Фаза 5

- **Frontend SPA** (Фаза 6)
- **Scheduler** (Фаза 7)
- **Telegram-нотификации** (Фаза 7)
- **Реальное подключение к FunPay** (Фаза 7)
- **Bump/Pause/Activate lot через API** — требуют ChatGateway注入ения (можно добавить, но FunPayChatGateway требует golden_key). Заглушки в lot_sync.py можно вызвать через API, но без реального FunPay это будет FakeChatGateway.
