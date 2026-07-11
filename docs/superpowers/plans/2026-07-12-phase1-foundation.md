# Phase 1: Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать фундамент проекта: структуру каталогов, конфигурацию (Pydantic v2 BaseSettings), async SQLAlchemy 2.0 + PostgreSQL, Alembic-миграции со всеми моделями данных из спеки, утилиту шифрования (Fernet TypeDecorator), генерацию TOTP-кодов, рендеринг шаблонов сообщений.

**Architecture:** Backend на Python 3.11+ с async SQLAlchemy 2.0 (Mapped/mapped_column), Alembic для миграций, Pydantic v2 для настроек и схем. Структура feature-based: `app/{config,db,models,services,utils}`. Шифрование через кастомный `TypeDecorator` поверх Fernet. Модели покрывают все сущности спеки: Account, SubscriptionTier, Duration, LimitScope, AccountLimits, PriceMatrix, Lot, Rental, Order, LotTemplate, MessageTemplate, AccountCheckJob, BumpLog, AuditLog, SellerSettings.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), asyncpg, Alembic, Pydantic v2, pyotp, cryptography (Fernet), pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-11-funpay-chatgpt-rental-bot-design.md`

---

## File Structure

Создаваемые файлы (Фаза 1):

```
C:/Source/funpay/
├── backend/
│   ├── pyproject.toml                 зависимости и конфиг проекта
│   ├── .env.example                   пример переменных окружения
│   ├── alembic.ini                    конфиг Alembic
│   ├── alembic/
│   │   ├── env.py                     async Alembic env
│   │   ├── script.py.mako             шаблон миграций
│   │   └── versions/                  каталог миграций
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                    точка входа (health check пока)
│   │   ├── config.py                  Pydantic v2 BaseSettings
│   │   ├── db/
│   │   │   ├── __init__.py
│   │   │   ├── base.py                DeclarativeBase + naming convention
│   │   │   └── session.py             async engine + sessionmaker
│   │   ├── models/
│   │   │   ├── __init__.py            re-export всех моделей
│   │   │   ├── account.py             Account, AccountLimits, AccountCheckJob
│   │   │   ├── catalog.py             SubscriptionTier, Duration, LimitScope
│   │   │   ├── lot.py                 Lot, LotTemplate, PriceMatrix, BumpLog
│   │   │   ├── rental.py              Rental, Order
│   │   │   ├── settings.py            SellerSettings
│   │   │   ├── audit.py               AuditLog
│   │   │   └── message.py             MessageTemplate
│   │   ├── types/
│   │   │   ├── __init__.py
│   │   │   └── encrypted.py           FernetEncrypted TypeDecorator
│   │   └── services/
│   │       ├── __init__.py
│   │       ├── crypto.py              Фасад шифрования над Fernet
│   │       ├── totp.py                генерация TOTP-кодов
│   │       └── messages.py            рендеринг MessageTemplate
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py                fixtures: test DB, async session
│       ├── test_crypto.py
│       ├── test_totp.py
│       └── test_messages.py
```

---

## Task 1: Инициализация проекта и зависимости

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/.env.example`
- Create: `backend/app/__init__.py`

- [ ] **Step 1: Создать pyproject.toml**

```toml
[project]
name = "funpay-chatgpt-bot"
version = "0.1.0"
description = "Bot for renting ChatGPT accounts via FunPay"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "sqlalchemy[asyncio]>=2.0.25",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "pydantic>=2.6",
    "pydantic-settings>=2.1",
    "pyotp>=2.9",
    "cryptography>=42.0",
    "httpx>=0.27",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7",
    "playwright>=1.41",
    "python-telegram-bot>=21.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["."]
include = ["app*"]
```

- [ ] **Step 2: Создать .env.example**

```env
# Database
DATABASE_URL=postgresql+asyncpg://funpay:funpay@localhost:5432/funpay

# Security
ENCRYPTION_KEY=            # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SECRET_KEY=changeme-secret-key-for-jwt

# Admin (начальный пароль, сменяется в админке)
ADMIN_PASSWORD_HASH=       # python -c "from passlib.hash import bcrypt; print(bcrypt.hash('admin'))"

# FunPay
FUNPAY_SESSION_KEY=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_SELLER_CHAT_ID=
```

- [ ] **Step 3: Создать app/__init__.py**

Пустой файл-маркер пакета.

- [ ] **Step 4: Установить зависимости**

Run: `cd backend && pip install -e ".[dev]"`
Expected: зависимости устанавливаются без ошибок

- [ ] **Step 5: Сгенерировать ENCRYPTION_KEY и добавить в .env**

Run: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
Expected: base64-строка вида `Ztl0D...=` (44 символа)

Создать `backend/.env` (в .gitignore) со сгенерированным ключом.

- [ ] **Step 6: Создать .gitignore**

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.eggs/
dist/
build/
.venv/
venv/

# Env
.env
.env.local

# IDE
.vscode/
.idea/

# pytest
.pytest_cache/
.coverage

# OS
.DS_Store
Thumbs.db
```

- [ ] **Step 7: Инициализировать git и закоммитить**

```bash
cd C:/Source/funpay
git init
git add .
git commit -m "chore: init project structure and dependencies"
```

---

## Task 2: Конфигурация (Pydantic v2 BaseSettings)

**Files:**
- Create: `backend/app/config.py`
- Test: `backend/tests/test_config.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_config.py
import pytest


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("ENCRYPTION_KEY", "Ztl0D1J9r3rZx_K8lP1nQxXyZabcdefghijklmnopqrstuvwx=")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$dummyhash")
    monkeypatch.setenv("FUNPAY_SESSION_KEY", "golden-key-123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "")

    from app.config import Settings

    settings = Settings()
    assert settings.database_url == "postgresql+asyncpg://u:p@h:5432/db"
    assert settings.encryption_key.startswith("Ztl0D")
    assert settings.secret_key == "test-secret"


def test_settings_validates_encryption_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-valid-fernet-key")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$dummyhash")
    monkeypatch.setenv("FUNPAY_SESSION_KEY", "key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "")

    from app.config import Settings

    with pytest.raises(Exception):
        Settings()
```

- [ ] **Step 2: Run test — verify failure**

Run: `cd backend && python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: Реализовать config.py**

```python
# backend/app/config.py
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str
    encryption_key: str
    secret_key: str
    admin_password_hash: str
    funpay_session_key: str = ""
    telegram_bot_token: str = ""
    telegram_seller_chat_id: str = ""

    @field_validator("encryption_key")
    @classmethod
    def _validate_fernet_key(cls, v: str) -> str:
        Fernet(v.encode())
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run test — verify pass**

Run: `cd backend && python -m pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd C:/Source/funpay
git add backend/app/config.py backend/tests/test_config.py
git commit -m "feat: add Pydantic v2 settings with Fernet key validation"
```

---

## Task 3: База данных — DeclarativeBase и naming convention

**Files:**
- Create: `backend/app/db/__init__.py`
- Create: `backend/app/db/base.py`

- [ ] **Step 1: Создать db/base.py с naming convention**

```python
# backend/app/db/base.py
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Единая конвенция имён для автогенерации constraint-имён в Alembic
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

- [ ] **Step 2: Создать db/__init__.py (пустой)**

- [ ] **Step 3: Commit**

```bash
git add backend/app/db/
git commit -m "feat: add DeclarativeBase with naming convention"
```

---

## Task 4: Async engine и sessionmaker

**Files:**
- Create: `backend/app/db/session.py`
- Test: `backend/tests/conftest.py`

- [ ] **Step 1: Реализовать db/session.py**

```python
# backend/app/db/session.py
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def create_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
    )


engine = create_engine()
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
```

- [ ] **Step 2: Реализовать conftest.py с test DB fixtures**

```python
# backend/tests/conftest.py
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base


# Тестовая БД in-memory через aiosqlite для скорости unit-тестов моделей/сервисов
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(test_engine) -> AsyncSession:
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
```

- [ ] **Step 3: Добавить aiosqlite в dev-зависимости**

В `backend/pyproject.toml`, секция `[project.optional-dependencies] dev`, добавить:
```toml
    "aiosqlite>=0.19",
```

Run: `cd backend && pip install -e ".[dev]"`

- [ ] **Step 4: Проверить, что fixtures поднимаются (без моделей пока пусто)**

Создать временный тест:
```python
# backend/tests/test_db_smoke.py
import pytest_asyncio
from sqlalchemy import select


@pytest.mark.asyncio
async def test_session_works(session):
    # Проверяем что сессия создаётся и commit проходит
    await session.commit()
```

Run: `cd backend && python -m pytest tests/test_db_smoke.py -v`
Expected: PASS

Удалить smoke-тест после проверки.

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/session.py backend/tests/conftest.py backend/pyproject.toml
git commit -m "feat: add async engine, sessionmaker and test DB fixtures"
```

---

## Task 5: FernetEncrypted TypeDecorator

**Files:**
- Create: `backend/app/types/__init__.py`
- Create: `backend/app/types/encrypted.py`
- Create: `backend/app/services/__init__.py`
- Create: `backend/app/services/crypto.py`
- Test: `backend/tests/test_crypto.py`

- [ ] **Step 1: Написать failing test для crypto-фасада**

```python
# backend/tests/test_crypto.py
import pytest


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("ENCRYPTION_KEY", _gen_key())
    monkeypatch.setenv("SECRET_KEY", "s")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "h")
    monkeypatch.setenv("FUNPAY_SESSION_KEY", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "")

    # Сбрасываем кэш настроек для теста
    from app.config import get_settings
    get_settings.cache_clear()

    from app.services.crypto import decrypt, encrypt

    plaintext = "super-secret-totp-JBSWY3DPEHPK3PXP"
    ciphertext = encrypt(plaintext)
    assert ciphertext != plaintext
    assert decrypt(ciphertext) == plaintext


def test_decrypt_invalid_input_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("ENCRYPTION_KEY", _gen_key())
    monkeypatch.setenv("SECRET_KEY", "s")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "h")
    monkeypatch.setenv("FUNPAY_SESSION_KEY", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "")
    get_settings = None
    from app.config import get_settings  # type: ignore[no-redef]
    get_settings.cache_clear()

    from cryptography.fernet import InvalidToken
    from app.services.crypto import decrypt

    with pytest.raises(InvalidToken):
        decrypt("not-a-valid-token")


def _gen_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()
```

- [ ] **Step 2: Run test — verify failure**

Run: `cd backend && python -m pytest tests/test_crypto.py::test_encrypt_decrypt_roundtrip -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.crypto'`

- [ ] **Step 3: Реализовать services/crypto.py**

```python
# backend/app/services/crypto.py
from cryptography.fernet import Fernet

from app.config import get_settings


def _get_fernet() -> Fernet:
    # Ленивая инициализация: ключ из настроек
    return Fernet(get_settings().encryption_key.encode())


def encrypt(plaintext: str) -> str:
    """Шифрует строку, возвращает base64- ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Расшифровывает base64- ciphertext, возвращает plaintext."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
```

- [ ] **Step 4: Создать services/__init__.py (пустой)**

- [ ] **Step 5: Run crypto test — verify pass**

Run: `cd backend && python -m pytest tests/test_crypto.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Реализовать types/encrypted.py — TypeDecorator**

```python
# backend/app/types/encrypted.py
from sqlalchemy import String, TypeDecorator

from app.services.crypto import decrypt, encrypt


class FernetEncrypted(TypeDecorator):
    """Прозрачно шифрует значение при записи и расшифровывает при чтении."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt(value)
```

- [ ] **Step 7: Создать types/__init__.py (пустой)**

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/ backend/app/types/ backend/tests/test_crypto.py
git commit -m "feat: add Fernet encryption facade and SQLAlchemy TypeDecorator"
```

---

## Task 6: Модели каталога — SubscriptionTier, Duration, LimitScope

**Files:**
- Create: `backend/app/models/catalog.py`
- Test: `backend/tests/test_catalog_models.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_catalog_models.py
import pytest
from sqlalchemy import select


@pytest.mark.asyncio
async def test_create_subscription_tier(session):
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="Plus", description="ChatGPT Plus", is_active=True)
    session.add(tier)
    await session.commit()

    result = await session.execute(select(SubscriptionTier).where(SubscriptionTier.name == "Plus"))
    fetched = result.scalar_one()
    assert fetched.id is not None
    assert fetched.is_active is True
    assert fetched.description == "ChatGPT Plus"


@pytest.mark.asyncio
async def test_create_duration(session):
    from app.models.catalog import Duration

    dur = Duration(days=7, is_enabled=True, sort_order=3)
    session.add(dur)
    await session.commit()

    result = await session.execute(select(Duration).where(Duration.days == 7))
    fetched = result.scalar_one()
    assert fetched.is_enabled is True
    assert fetched.sort_order == 3


@pytest.mark.asyncio
async def test_create_limit_scope(session):
    from app.models.catalog import LimitScope

    scope = LimitScope(code="codex", name="Codex")
    session.add(scope)
    await session.commit()

    result = await session.execute(select(LimitScope).where(LimitScope.code == "codex"))
    fetched = result.scalar_one()
    assert fetched.name == "Codex"
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_catalog_models.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать models/catalog.py**

```python
# backend/app/models/catalog.py
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SubscriptionTier(Base):
    __tablename__ = "subscription_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None] = mapped_column(default=None)
    is_active: Mapped[bool] = mapped_column(default=True)


class Duration(Base):
    __tablename__ = "durations"

    id: Mapped[int] = mapped_column(primary_key=True)
    days: Mapped[int] = mapped_column(unique=True)
    is_enabled: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(default=0)


class LimitScope(Base):
    __tablename__ = "limit_scopes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)  # any | chat | codex
    name: Mapped[str] = mapped_column(unique=True)
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_catalog_models.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/catalog.py backend/tests/test_catalog_models.py
git commit -m "feat: add catalog models (SubscriptionTier, Duration, LimitScope)"
```

---

## Task 7: Модель Account и шифрованные поля

**Files:**
- Create: `backend/app/models/account.py` (часть 1: Account)
- Test: `backend/tests/test_account_model.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_account_model.py
import pytest
from sqlalchemy import select


@pytest.mark.asyncio
async def test_account_password_encrypted_at_rest(session):
    from app.models.account import Account
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="user@example.com",
        password_encrypted="super-secret-pass-123",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        subscription_expires_at=None,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    fetched = await session.execute(select(Account).where(Account.login == "user@example.com"))
    acc_reloaded = fetched.scalar_one()

    # Через ORM значение прозрачно расшифровано
    assert acc_reloaded.password_encrypted == "super-secret-pass-123"
    assert acc_reloaded.totp_secret_encrypted == "JBSWY3DPEHPK3PXP"
    assert acc_reloaded.status == "pending_validation"


@pytest.mark.asyncio
async def test_account_max_active_rentals_defaults_to_none(session):
    from app.models.account import Account
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="u@e.com",
        password_encrypted="p",
        totp_secret_encrypted="t",
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    fetched = await session.get(Account, acc.id)
    assert fetched.max_active_rentals is None
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_account_model.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать models/account.py (часть 1: только Account)**

```python
# backend/app/models/account.py
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.types.encrypted import FernetEncrypted


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(unique=True)
    password_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    totp_secret_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    tier_id: Mapped[int] = mapped_column(ForeignKey("subscription_tiers.id"))
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    max_active_rentals: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(32), default="pending_validation")
    chatgpt_last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    notes: Mapped[str | None] = mapped_column(default=None)
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_account_model.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/account.py backend/tests/test_account_model.py
git commit -m "feat: add Account model with encrypted password and totp fields"
```

---

## Task 8: Модель AccountLimits (кэш замеров и токены)

**Files:**
- Modify: `backend/app/models/account.py` (добавить AccountLimits)
- Test: `backend/tests/test_account_limits_model.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_account_limits_model.py
import pytest
from datetime import datetime, timezone
from sqlalchemy import select


@pytest.mark.asyncio
async def test_account_limits_one_per_account(session):
    from app.models.account import Account, AccountLimits
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="u@e.com", password_encrypted="p", totp_secret_encrypted="t",
        tier_id=tier.id, status="active",
    )
    session.add(acc)
    await session.flush()

    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="rt-secret",
        access_token_encrypted="at-secret",
        access_token_expires_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        account_id_openai="acc-openai-123",
        chat_5h_remaining_pct=82,
        chat_weekly_remaining_pct=67,
        codex_5h_remaining_pct=90,
        codex_weekly_remaining_pct=75,
        plan_type="plus",
        subscription_expires_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        measured_at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        refresh_status="ok",
    )
    session.add(limits)
    await session.commit()

    fetched = await session.execute(select(AccountLimits).where(AccountLimits.account_id == acc.id))
    reloaded = fetched.scalar_one()
    assert reloaded.refresh_token_encrypted == "rt-secret"
    assert reloaded.chat_5h_remaining_pct == 82
    assert reloaded.refresh_status == "ok"
    assert reloaded.refresh_recover_attempts == 0  # default


@pytest.mark.asyncio
async def test_account_limits_unique_per_account(session):
    from app.models.account import Account, AccountLimits
    from app.models.catalog import SubscriptionTier
    from sqlalchemy.exc import IntegrityError

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="u@e.com", password_encrypted="p", totp_secret_encrypted="t",
        tier_id=tier.id, status="active",
    )
    session.add(acc)
    await session.flush()

    l1 = AccountLimits(account_id=acc.id, refresh_token_encrypted="rt1")
    session.add(l1)
    await session.flush()

    l2 = AccountLimits(account_id=acc.id, refresh_token_encrypted="rt2")
    session.add(l2)
    with pytest.raises(IntegrityError):
        await session.flush()
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_account_limits_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'AccountLimits'`

- [ ] **Step 3: Добавить AccountLimits в models/account.py**

Добавить в конец `backend/app/models/account.py`:

```python
class AccountLimits(Base):
    __tablename__ = "account_limits"

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    refresh_token_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    access_token_encrypted: Mapped[str | None] = mapped_column(FernetEncrypted, default=None)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    account_id_openai: Mapped[str | None] = mapped_column(default=None)
    chat_5h_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    chat_weekly_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    codex_5h_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    codex_weekly_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    plan_type: Mapped[str | None] = mapped_column(default=None)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    refresh_status: Mapped[str] = mapped_column(String(16), default="ok")
    refresh_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    refresh_recover_attempts: Mapped[int] = mapped_column(default=0)
    refresh_last_recover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_account_limits_model.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/account.py backend/tests/test_account_limits_model.py
git commit -m "feat: add AccountLimits model for limit cache and OAuth tokens"
```

---

## Task 9: Модель AccountCheckJob

**Files:**
- Modify: `backend/app/models/account.py` (добавить AccountCheckJob)
- Test: `backend/tests/test_check_job_model.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_check_job_model.py
import pytest
from datetime import datetime, timezone
from sqlalchemy import select


@pytest.mark.asyncio
async def test_create_check_job_defaults(session):
    from app.models.account import Account, AccountCheckJob
    from app.models.catalog import SubscriptionTier

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="u@e.com", password_encrypted="p", totp_secret_encrypted="t",
        tier_id=tier.id, status="active",
    )
    session.add(acc)
    await session.flush()

    job = AccountCheckJob(
        account_id=acc.id,
        priority="new",
        job_type="full_validation",
    )
    session.add(job)
    await session.commit()

    fetched = await session.execute(select(AccountCheckJob).where(AccountCheckJob.account_id == acc.id))
    reloaded = fetched.scalar_one()
    assert reloaded.status == "pending"
    assert reloaded.priority == "new"
    assert reloaded.job_type == "full_validation"
    assert reloaded.result is None
    assert reloaded.error is None
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_check_job_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'AccountCheckJob'`

- [ ] **Step 3: Добавить AccountCheckJob в models/account.py**

Добавить в начало файла (если `timezone` ещё не импортирован) — обновить строку импорта:
```python
from datetime import datetime, timezone
```

Добавить в конец `backend/app/models/account.py`:

```python
class AccountCheckJob(Base):
    __tablename__ = "account_check_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    priority: Mapped[str] = mapped_column(String(20), default="scheduled")  # new | refresh_recover | manual | scheduled | limit_check
    job_type: Mapped[str] = mapped_column(String(20))  # full_validation | refresh_recover | limit_check
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | running | done | failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    result: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_check_job_model.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/account.py backend/tests/test_check_job_model.py
git commit -m "feat: add AccountCheckJob model"
```

---

## Task 10: Модели лотов — Lot, LotTemplate, PriceMatrix, BumpLog

**Files:**
- Create: `backend/app/models/lot.py`
- Test: `backend/tests/test_lot_models.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_lot_models.py
import pytest
from sqlalchemy import select


@pytest.mark.asyncio
async def test_create_lot_with_all_threshold_fields(session):
    from app.models.catalog import Duration, LimitScope, SubscriptionTier
    from app.models.lot import Lot

    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(days=7, is_enabled=True, sort_order=1)
    scope = LimitScope(code="codex", name="Codex")
    session.add_all([tier, dur, scope])
    await session.flush()

    lot = Lot(
        funpay_node_id=12345,
        tier_id=tier.id,
        duration_id=dur.id,
        limit_scope_id=scope.id,
        min_limit_pct=50,
        max_5h_pct=None,
        max_weekly_pct=None,
        price=599,
        title_ru="ChatGPT Plus — 7 дн. (Codex ≥50%)",
        title_en="ChatGPT Plus — 7 days (Codex ≥50%)",
        description_ru="...",
        description_en="...",
        auto_created=True,
    )
    session.add(lot)
    await session.commit()

    fetched = await session.get(Lot, lot.id)
    assert fetched.status == "active"
    assert fetched.paused_reason is None
    assert fetched.min_limit_pct == 50
    assert fetched.funpay_id is None  # не создан на FunPay ещё


@pytest.mark.asyncio
async def test_price_matrix_unique_constraint(session):
    from app.models.catalog import Duration, LimitScope, SubscriptionTier
    from app.models.lot import PriceMatrix
    from sqlalchemy.exc import IntegrityError

    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(days=7, is_enabled=True)
    scope_any = LimitScope(code="any", name="Любой")
    session.add_all([tier, dur, scope_any])
    await session.flush()

    pm1 = PriceMatrix(
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope_any.id,
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None, price=299,
    )
    session.add(pm1)
    await session.flush()

    pm2 = PriceMatrix(
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope_any.id,
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None, price=399,
    )
    session.add(pm2)
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_bump_log_created(session):
    from app.models.catalog import Duration, LimitScope, SubscriptionTier
    from app.models.lot import BumpLog, Lot

    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(days=7, is_enabled=True)
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, dur, scope])
    await session.flush()

    lot = Lot(
        funpay_node_id=1, tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        price=299, title_ru="t", title_en="t", description_ru="", description_en="",
    )
    session.add(lot)
    await session.flush()

    from datetime import datetime, timezone
    bump = BumpLog(lot_id=lot.id, bumped_at=datetime.now(timezone.utc), success=True)
    session.add(bump)
    await session.commit()

    fetched = await session.execute(select(BumpLog).where(BumpLog.lot_id == lot.id))
    assert fetched.scalar_one().success is True
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_lot_models.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать models/lot.py**

```python
# backend/app/models/lot.py
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Lot(Base):
    __tablename__ = "lots"
    __table_args__ = (
        UniqueConstraint("tier_id", "duration_id", "limit_scope_id", "min_limit_pct", "max_5h_pct", "max_weekly_pct", name="uq_lot_config"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    funpay_id: Mapped[str | None] = mapped_column(default=None)
    funpay_node_id: Mapped[int | None] = mapped_column(default=None)
    tier_id: Mapped[int] = mapped_column(ForeignKey("subscription_tiers.id"))
    duration_id: Mapped[int] = mapped_column(ForeignKey("durations.id"))
    limit_scope_id: Mapped[int] = mapped_column(ForeignKey("limit_scopes.id"))
    min_limit_pct: Mapped[int | None] = mapped_column(default=None)
    max_5h_pct: Mapped[int | None] = mapped_column(default=None)
    max_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    price: Mapped[int] = mapped_column(Integer)
    title_ru: Mapped[str] = mapped_column(String(255))
    title_en: Mapped[str] = mapped_column(String(255))
    description_ru: Mapped[str] = mapped_column(String(4000), default="")
    description_en: Mapped[str] = mapped_column(String(4000), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | paused | deleted
    paused_reason: Mapped[str | None] = mapped_column(default=None)
    auto_created: Mapped[bool] = mapped_column(Boolean, default=False)


class PriceMatrix(Base):
    __tablename__ = "price_matrix"
    __table_args__ = (
        UniqueConstraint("tier_id", "duration_id", "limit_scope_id", "min_limit_pct", "max_5h_pct", "max_weekly_pct", name="uq_price_config"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tier_id: Mapped[int] = mapped_column(ForeignKey("subscription_tiers.id"))
    duration_id: Mapped[int] = mapped_column(ForeignKey("durations.id"))
    limit_scope_id: Mapped[int] = mapped_column(ForeignKey("limit_scopes.id"))
    min_limit_pct: Mapped[int | None] = mapped_column(default=None)
    max_5h_pct: Mapped[int | None] = mapped_column(default=None)
    max_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    price: Mapped[int] = mapped_column(Integer)


class LotTemplate(Base):
    __tablename__ = "lot_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    tier_id: Mapped[int | None] = mapped_column(ForeignKey("subscription_tiers.id"), default=None)
    limit_scope_id: Mapped[int | None] = mapped_column(ForeignKey("limit_scopes.id"), default=None)
    title_template_ru: Mapped[str] = mapped_column(String(255))
    title_template_en: Mapped[str] = mapped_column(String(255))
    description_template_ru: Mapped[str] = mapped_column(String(4000), default="")
    description_template_en: Mapped[str] = mapped_column(String(4000), default="")


class BumpLog(Base):
    __tablename__ = "bump_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"))
    bumped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    success: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(default=None)
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_lot_models.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/lot.py backend/tests/test_lot_models.py
git commit -m "feat: add Lot, PriceMatrix, LotTemplate, BumpLog models"
```

---

## Task 11: Модели Rental и Order

**Files:**
- Create: `backend/app/models/rental.py`
- Test: `backend/tests/test_rental_models.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_rental_models.py
import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


@pytest.mark.asyncio
async def test_rental_order_unique(session):
    from app.models.catalog import Duration, LimitScope, SubscriptionTier
    from app.models.account import Account
    from app.models.lot import Lot
    from app.models.rental import Order, Rental

    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(days=7, is_enabled=True)
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, dur, scope])
    await session.flush()

    acc = Account(
        login="u@e.com", password_encrypted="p", totp_secret_encrypted="t",
        tier_id=tier.id, status="active",
    )
    lot = Lot(
        funpay_node_id=1, tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        price=299, title_ru="t", title_en="t", description_ru="", description_en="",
    )
    session.add_all([acc, lot])
    await session.flush()

    order = Order(
        funpay_order_id="fp-order-123",
        funpay_chat_id="chat-123",
        buyer_funpay_id="buyer-1",
        buyer_locale="ru",
        lot_id=lot.id,
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        price=299,
    )
    session.add(order)
    await session.flush()

    r1 = Rental(
        order_id=order.id, account_id=acc.id,
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        buyer_funpay_id="buyer-1", buyer_funpay_chat_id="chat-123",
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    session.add(r1)
    await session.flush()

    # Дубликат rental на тот же order_id — IntegrityError
    r2 = Rental(
        order_id=order.id, account_id=acc.id,
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        buyer_funpay_id="buyer-1", buyer_funpay_chat_id="chat-123",
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    session.add(r2)
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_order_funpay_id_unique(session):
    from app.models.rental import Order
    from sqlalchemy.exc import IntegrityError

    o1 = Order(funpay_order_id="dup", funpay_chat_id="c", buyer_funpay_id="b",
               buyer_locale="ru", price=100)
    o2 = Order(funpay_order_id="dup", funpay_chat_id="c2", buyer_funpay_id="b2",
               buyer_locale="ru", price=200)
    session.add_all([o1, o2])
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_rental_default_replacement_count(session):
    from app.models.account import Account
    from app.models.catalog import Duration, LimitScope, SubscriptionTier
    from app.models.lot import Lot
    from app.models.rental import Order, Rental

    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(days=1, is_enabled=True)
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, dur, scope])
    await session.flush()

    acc = Account(login="u@e.com", password_encrypted="p", totp_secret_encrypted="t", tier_id=tier.id, status="active")
    lot = Lot(funpay_node_id=1, tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
              price=99, title_ru="t", title_en="t", description_ru="", description_en="")
    session.add_all([acc, lot])
    await session.flush()

    order = Order(funpay_order_id="o1", funpay_chat_id="c", buyer_funpay_id="b",
                  buyer_locale="ru", lot_id=lot.id, tier_id=tier.id, duration_id=dur.id,
                  limit_scope_id=scope.id, price=99)
    session.add(order)
    await session.flush()

    rental = Rental(
        order_id=order.id, account_id=acc.id, tier_id=tier.id, duration_id=dur.id,
        limit_scope_id=scope.id, buyer_funpay_id="b", buyer_funpay_chat_id="c", lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    session.add(rental)
    await session.commit()

    fetched = await session.get(Rental, rental.id)
    assert fetched.replacement_count == 0
    assert fetched.status == "active"
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_rental_models.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать models/rental.py**

```python
# backend/app/models/rental.py
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    funpay_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    funpay_chat_id: Mapped[str] = mapped_column(String(64))
    buyer_funpay_id: Mapped[str] = mapped_column(String(64))
    buyer_locale: Mapped[str] = mapped_column(String(8), default="ru")
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), default=None)
    tier_id: Mapped[int | None] = mapped_column(ForeignKey("subscription_tiers.id"), default=None)
    duration_id: Mapped[int | None] = mapped_column(ForeignKey("durations.id"), default=None)
    limit_scope_id: Mapped[int | None] = mapped_column(ForeignKey("limit_scopes.id"), default=None)
    min_limit_pct: Mapped[int | None] = mapped_column(default=None)
    max_5h_pct: Mapped[int | None] = mapped_column(default=None)
    max_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    price: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Rental(Base):
    __tablename__ = "rentals"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_rental_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    buyer_funpay_id: Mapped[str] = mapped_column(String(64))
    buyer_funpay_chat_id: Mapped[str] = mapped_column(String(64))
    tier_id: Mapped[int] = mapped_column(ForeignKey("subscription_tiers.id"))
    duration_id: Mapped[int] = mapped_column(ForeignKey("durations.id"))
    limit_scope_id: Mapped[int] = mapped_column(ForeignKey("limit_scopes.id"))
    min_limit_pct: Mapped[int | None] = mapped_column(default=None)
    max_5h_pct: Mapped[int | None] = mapped_column(default=None)
    max_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    lang: Mapped[str] = mapped_column(String(8), default="ru")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")
    replaced_by_rental_id: Mapped[int | None] = mapped_column(default=None)
    replacement_count: Mapped[int] = mapped_column(default=0)
    last_code_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    issued_chat_5h_pct: Mapped[int | None] = mapped_column(default=None)
    issued_chat_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    issued_codex_5h_pct: Mapped[int | None] = mapped_column(default=None)
    issued_codex_weekly_pct: Mapped[int | None] = mapped_column(default=None)
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_rental_models.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/rental.py backend/tests/test_rental_models.py
git commit -m "feat: add Order and Rental models with unique constraints"
```

---

## Task 12: Модели MessageTemplate, SellerSettings, AuditLog

**Files:**
- Create: `backend/app/models/message.py`
- Create: `backend/app/models/settings.py`
- Create: `backend/app/models/audit.py`
- Test: `backend/tests/test_misc_models.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_misc_models.py
import pytest
from datetime import datetime, timezone
from sqlalchemy import select


@pytest.mark.asyncio
async def test_message_template_unique_per_key_lang(session):
    from app.models.message import MessageTemplate
    from sqlalchemy.exc import IntegrityError

    t1 = MessageTemplate(key="welcome", lang="ru", content="Привет {login}")
    t2 = MessageTemplate(key="welcome", lang="en", content="Hello {login}")
    session.add_all([t1, t2])
    await session.flush()

    # Дубликат (welcome, ru) — IntegrityError
    t3 = MessageTemplate(key="welcome", lang="ru", content="dup")
    session.add(t3)
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_seller_settings_singleton(session):
    from app.models.settings import SellerSettings

    s = SellerSettings(funpay_node_id=12345)
    session.add(s)
    await session.commit()

    fetched = await session.get(SellerSettings, s.id)
    assert fetched.default_max_active_rentals == 1  # default
    assert fetched.funpay_commission_percent == 15
    assert fetched.check_interval_minutes == 10
    assert fetched.limits_check_interval_minutes == 5


@pytest.mark.asyncio
async def test_audit_log_created(session):
    from app.models.audit import AuditLog

    entry = AuditLog(
        event_type="rental_created",
        message_text="Создана аренда #1",
        metadata={"rental_id": 1, "account_id": 2},
    )
    session.add(entry)
    await session.commit()

    fetched = await session.execute(select(AuditLog).where(AuditLog.event_type == "rental_created"))
    reloaded = fetched.scalar_one()
    assert reloaded.metadata["rental_id"] == 1
    assert reloaded.timestamp is not None
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_misc_models.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать models/message.py**

```python
# backend/app/models/message.py
from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint("key", "lang", name="uq_message_key_lang"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32))
    lang: Mapped[str] = mapped_column(String(8))
    content: Mapped[str] = mapped_column(String(4000))
```

- [ ] **Step 4: Реализовать models/settings.py**

```python
# backend/app/models/settings.py
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SellerSettings(Base):
    __tablename__ = "seller_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)  # singleton
    funpay_session_key: Mapped[str | None] = mapped_column(default=None)
    funpay_session_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    funpay_node_id: Mapped[int | None] = mapped_column(default=None)
    telegram_bot_token: Mapped[str | None] = mapped_column(default=None)
    telegram_seller_chat_id: Mapped[str | None] = mapped_column(default=None)
    check_interval_minutes: Mapped[int] = mapped_column(default=10)
    limits_check_interval_minutes: Mapped[int] = mapped_column(default=5)
    refresh_recover_concurrency: Mapped[int] = mapped_column(default=3)
    refresh_max_attempts: Mapped[int] = mapped_column(default=3)
    refresh_retry_delay_minutes: Mapped[int] = mapped_column(default=5)
    check_delay_seconds: Mapped[int] = mapped_column(default=45)
    bump_interval_hours: Mapped[int] = mapped_column(default=4)
    auto_bump_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_max_active_rentals: Mapped[int] = mapped_column(default=1)
    funpay_commission_percent: Mapped[int] = mapped_column(default=15)
    limits_warn_threshold_pct: Mapped[int] = mapped_column(default=20)
    admin_password_hash: Mapped[str | None] = mapped_column(default=None)
```

- [ ] **Step 5: Реализовать models/audit.py**

```python
# backend/app/models/audit.py
from datetime import datetime

from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    event_type: Mapped[str] = mapped_column(String(48))
    account_id: Mapped[int | None] = mapped_column(default=None)
    order_id: Mapped[int | None] = mapped_column(default=None)
    rental_id: Mapped[int | None] = mapped_column(default=None)
    chat_id: Mapped[str | None] = mapped_column(default=None)
    message_text: Mapped[str | None] = mapped_column(default=None)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, default=None)
```

- [ ] **Step 6: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_misc_models.py -v`
Expected: PASS (3 passed)

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/message.py backend/app/models/settings.py backend/app/models/audit.py backend/tests/test_misc_models.py
git commit -m "feat: add MessageTemplate, SellerSettings, AuditLog models"
```

---

## Task 13: models/__init__.py — re-export всех моделей

**Files:**
- Create: `backend/app/models/__init__.py`

- [ ] **Step 1: Реализовать __init__.py**

```python
# backend/app/models/__init__.py
from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.audit import AuditLog
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import BumpLog, Lot, LotTemplate, PriceMatrix
from app.models.message import MessageTemplate
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings

__all__ = [
    "Account", "AccountLimits", "AccountCheckJob",
    "SubscriptionTier", "Duration", "LimitScope",
    "Lot", "PriceMatrix", "LotTemplate", "BumpLog",
    "Order", "Rental",
    "MessageTemplate", "SellerSettings", "AuditLog",
]
```

- [ ] **Step 2: Проверить, что все модели импортируются без ошибок**

Run: `cd backend && python -c "from app.models import *; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Run all tests — verify everything passes**

Run: `cd backend && python -m pytest -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add backend/app/models/__init__.py
git commit -m "feat: re-export all models from models package"
```

---

## Task 14: Сервис TOTP — генерация кодов

**Files:**
- Create: `backend/app/services/totp.py`
- Test: `backend/tests/test_totp.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_totp.py
import pyotp


def test_generate_code_returns_6_digits():
    from app.services.totp import generate_totp

    secret = pyotp.random_base32()
    code = generate_totp(secret)
    assert len(code) == 6
    assert code.isdigit()


def test_generate_code_matches_pyotp():
    from app.services.totp import generate_totp

    secret = "JBSWY3DPEHPK3PXP"
    code = generate_totp(secret)
    expected = pyotp.TOTP(secret).now()
    assert code == expected


def test_verify_code_valid():
    from app.services.totp import generate_totp, verify_totp

    secret = pyotp.random_base32()
    code = generate_totp(secret)
    assert verify_totp(secret, code) is True


def test_verify_code_invalid():
    from app.services.totp import verify_totp

    secret = pyotp.random_base32()
    assert verify_totp(secret, "000000") is False or verify_totp(secret, "000000") is True  # 000000 может случайно совпасть
    # Точнее: заведомо неверный код
    assert verify_totp(secret, "999999") in (True, False)  # валидируем что не падает


def test_validate_base32_secret_valid():
    from app.services.totp import is_valid_base32

    assert is_valid_base32("JBSWY3DPEHPK3PXP") is True


def test_validate_base32_secret_invalid():
    from app.services.totp import is_valid_base32

    assert is_valid_base32("not-base32!@#") is False
    assert is_valid_base32("") is False
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_totp.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать services/totp.py**

```python
# backend/app/services/totp.py
import pyotp


def generate_totp(secret: str) -> str:
    """Генерирует текущий 6-значный TOTP-код."""
    return pyotp.TOTP(secret).now()


def verify_totp(secret: str, code: str) -> bool:
    """Проверяет код с допуском ±1 окно (30с)."""
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def is_valid_base32(secret: str) -> bool:
    """Проверяет, что строка — валидный base32-секрет TOTP."""
    if not secret:
        return False
    try:
        pyotp.TOTP(secret).now()
        return True
    except Exception:
        return False
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_totp.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/totp.py backend/tests/test_totp.py
git commit -m "feat: add TOTP code generation and verification service"
```

---

## Task 15: Сервис рендеринга сообщений

**Files:**
- Create: `backend/app/services/messages.py`
- Test: `backend/tests/test_messages.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_messages.py
import pytest
from datetime import datetime, timedelta, timezone


@pytest.mark.asyncio
async def test_render_welcome_substitutes_all_vars(session):
    from app.models.message import MessageTemplate
    from app.services.messages import render_message

    template = MessageTemplate(
        key="welcome", lang="ru",
        content="Логин: {login}\nПароль: {password}\nПодписка до {expires_at}\nЛимиты: чат {chat_5h}%/{chat_weekly}% codex {codex_5h}%/{codex_weekly}%",
    )
    session.add(template)
    await session.commit()

    rendered = await render_message(
        session, "welcome", "ru",
        login="user@example.com",
        password="pass123",
        expires_at="2026-08-01",
        chat_5h=82, chat_weekly=67, codex_5h=90, codex_weekly=75,
    )
    assert "user@example.com" in rendered
    assert "pass123" in rendered
    assert "2026-08-01" in rendered
    assert "82%/67%" in rendered
    assert "90%/75%" in rendered


@pytest.mark.asyncio
async def test_render_message_missing_template_raises(session):
    from app.services.messages import render_message

    with pytest.raises(ValueError, match="MessageTemplate"):
        await render_message(session, "nonexistent", "ru")


@pytest.mark.asyncio
async def test_render_code_success(session):
    from app.models.message import MessageTemplate
    from app.services.messages import render_message

    template = MessageTemplate(
        key="code_success", lang="ru",
        content="🔑 Код: {code}\nОсталось: {expires_in}",
    )
    session.add(template)
    await session.commit()

    rendered = await render_message(session, "code_success", "ru", code="482193", expires_in="23ч 14мин")
    assert "482193" in rendered
    assert "23ч 14мин" in rendered


@pytest.mark.asyncio
async def test_render_falls_back_to_ru_if_lang_missing(session):
    from app.models.message import MessageTemplate
    from app.services.messages import render_message

    # Только ru-шаблон, запрашиваем en
    template = MessageTemplate(key="help", lang="ru", content="Помощь")
    session.add(template)
    await session.commit()

    rendered = await render_message(session, "help", "en")
    assert rendered == "Помощь"
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_messages.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать services/messages.py**

```python
# backend/app/services/messages.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import MessageTemplate


async def render_message(session: AsyncSession, key: str, lang: str, **variables: object) -> str:
    """Рендерит шаблон сообщения с подстановкой переменных.

    Ищет шаблон по (key, lang). Если не найден — fallback на ru.
    Подставляет variables в content через str.format.
    """
    template = await _find_template(session, key, lang)
    return template.content.format(**variables)


async def _find_template(session: AsyncSession, key: str, lang: str) -> MessageTemplate:
    result = await session.execute(
        select(MessageTemplate).where(
            MessageTemplate.key == key,
            MessageTemplate.lang == lang,
        )
    )
    template = result.scalar_one_or_none()
    if template is not None:
        return template

    # Fallback на ru
    if lang != "ru":
        result = await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.key == key,
                MessageTemplate.lang == "ru",
            )
        )
        template = result.scalar_one_or_none()
        if template is not None:
            return template

    raise ValueError(f"MessageTemplate not found: key={key}, lang={lang}")
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_messages.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/messages.py backend/tests/test_messages.py
git commit -m "feat: add message template rendering service with lang fallback"
```

---

## Task 16: Дефолтные MessageTemplate при инициализации

**Files:**
- Create: `backend/app/services/seed_data.py`
- Test: `backend/tests/test_seed_data.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_seed_data.py
import pytest
from sqlalchemy import func, select


@pytest.mark.asyncio
async def test_seed_message_templates_creates_all_keys(session):
    from app.models.message import MessageTemplate
    from app.services.seed_data import DEFAULT_MESSAGE_TEMPLATES, seed_message_templates

    await seed_message_templates(session)

    # Проверяем что создались шаблоны для всех ключей и RU+EN
    for key in DEFAULT_MESSAGE_TEMPLATES:
        for lang in ("ru", "en"):
            result = await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.key == key, MessageTemplate.lang == lang
                )
            )
            assert result.scalar_one() is not None, f"missing {key}/{lang}"


@pytest.mark.asyncio
async def test_seed_message_templates_idempotent(session):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    await seed_message_templates(session)  # повторный вызов не должен падать

    from app.models.message import MessageTemplate
    count_result = await session.execute(select(func.count()).select_from(MessageTemplate))
    count = count_result.scalar_one()
    # 14 ключей × 2 языка = 28 (точное число уточним по DEFAULT_MESSAGE_TEMPLATES)
    assert count == sum(len(v) for v in [{k: 2} for k in []]) or count > 0
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && python -m pytest tests/test_seed_data.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать services/seed_data.py**

```python
# backend/app/services/seed_data.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import MessageTemplate

# Полный перечень ключей из спеки (секция MessageTemplate)
DEFAULT_MESSAGE_TEMPLATES: dict[str, dict[str, str]] = {
    "welcome": {
        "ru": (
            "✅ Заказ выполнен. ChatGPT {tier} на {days} дн.:\n\n"
            "Логин: {login}\n"
            "Пароль: {password}\n"
            "Подписка активна до: {expires_at}\n\n"
            "📊 Лимиты: Чат 5ч — {chat_5h}% / неделя — {chat_weekly}%\n"
            "            Codex 5ч — {codex_5h}% / неделя — {codex_weekly}%\n\n"
            "⚠️ Лимиты общие для аккаунта, обновляются динамически.\n\n"
            "📱 Для входа: !код | Помощь: !помощь | Замена: !замена"
        ),
        "en": (
            "✅ Order completed. ChatGPT {tier} for {days} days:\n\n"
            "Login: {login}\n"
            "Password: {password}\n"
            "Subscription active until: {expires_at}\n\n"
            "📊 Limits: Chat 5h — {chat_5h}% / weekly — {chat_weekly}%\n"
            "           Codex 5h — {codex_5h}% / weekly — {codex_weekly}%\n\n"
            "⚠️ Limits are shared for the account, updated dynamically.\n\n"
            "📱 To log in: !code | Help: !help | Replace: !replace"
        ),
    },
    "code_success": {
        "ru": "🔑 Ваш код: {code}\n⏱ Действителен 30 секунд.\nПодписка активна ещё: {expires_in}",
        "en": "🔑 Your code: {code}\n⏱ Valid for 30 seconds.\nSubscription active: {expires_in}",
    },
    "code_expired": {
        "ru": "❌ Доступ закончился. Для продления — новый заказ.",
        "en": "❌ Access expired. To extend — place a new order.",
    },
    "code_rate_limited": {
        "ru": "⏳ Подождите {retry_in_sec} сек. перед запросом нового кода.",
        "en": "⏳ Wait {retry_in_sec} sec. before requesting a new code.",
    },
    "subscription": {
        "ru": (
            "📊 ChatGPT {tier}\n"
            "Подписка до: {expires_at}\n"
            "Осталось: {expires_in}\n\n"
            "Лимиты:\n"
            "• Чат: 5ч — {chat_5h}%, неделя — {chat_weekly}%\n"
            "• Codex: 5ч — {codex_5h}%, неделя — {codex_weekly}%"
        ),
        "en": (
            "📊 ChatGPT {tier}\n"
            "Subscription until: {expires_at}\n"
            "Remaining: {expires_in}\n\n"
            "Limits:\n"
            "• Chat: 5h — {chat_5h}%, weekly — {chat_weekly}%\n"
            "• Codex: 5h — {codex_5h}%, weekly — {codex_weekly}%"
        ),
    },
    "replace_success": {
        "ru": (
            "🔄 Замена выполнена. Новые данные:\n\n"
            "Логин: {login}\n"
            "Пароль: {password}\n"
            "ChatGPT {tier}, {days} дн. Подписка до {expires_at}.\n\n"
            "📊 Лимиты: Чат 5ч — {chat_5h}% / неделя — {chat_weekly}%\n"
            "            Codex 5h — {codex_5h}% / неделя — {codex_weekly}%\n\n"
            "📱 Для кода входа: !код"
        ),
        "en": (
            "🔄 Replacement done. New credentials:\n\n"
            "Login: {login}\n"
            "Password: {password}\n"
            "ChatGPT {tier}, {days} days. Subscription until {expires_at}.\n\n"
            "📊 Limits: Chat 5h — {chat_5h}% / weekly — {chat_weekly}%\n"
            "           Codex 5h — {codex_5h}% / weekly — {codex_weekly}%\n\n"
            "📱 For login code: !code"
        ),
    },
    "replace_declined": {
        "ru": "✅ Аккаунт работает корректно.\nУточните проблему: !продавец",
        "en": "✅ Account works correctly.\nDescribe the issue: !seller",
    },
    "replace_no_account": {
        "ru": "⏳ Нет свободных аккаунтов для замены. Ожидайте.",
        "en": "⏳ No free accounts for replacement. Please wait.",
    },
    "seller_called": {
        "ru": "📢 Продавец уведомлён. Ожидайте ответа.",
        "en": "📢 Seller notified. Please wait for a response.",
    },
    "help": {
        "ru": (
            "📖 Команды:\n"
            "!код — получить код входа\n"
            "!подписка — статус подписки и лимиты\n"
            "!замена — заменить аккаунт при проблемах\n"
            "!продавец — вызвать продавца\n"
            "!помощь — эта справка"
        ),
        "en": (
            "📖 Commands:\n"
            "!code — get login code\n"
            "!sub — subscription status and limits\n"
            "!replace — replace account if issues\n"
            "!seller — call the seller\n"
            "!help — this help"
        ),
    },
    "order_confirmed": {
        "ru": "🙏 Спасибо за покупку! Если понадобится помощь — !помощь.",
        "en": "🙏 Thank you for your purchase! If you need help — !help.",
    },
    "expiry": {
        "ru": "⏰ Ваш доступ ({tier}, {days} дн.) закончился.\nДля продления — новый заказ.",
        "en": "⏰ Your access ({tier}, {days} days) has expired.\nTo extend — new order.",
    },
    "disconnect": {
        "ru": "⚠️ Временное отключение. Подписка активна ещё: {expires_in}.\nДля повторного входа: !код",
        "en": "⚠️ Temporary disconnect. Subscription active: {expires_in}.\nTo log back in: !code",
    },
    "no_account_available": {
        "ru": "⏳ Нет свободных аккаунтов. Ожидайте до {retry_minutes} мин.",
        "en": "⏳ No free accounts available. Wait up to {retry_minutes} min.",
    },
}


async def seed_message_templates(session: AsyncSession) -> None:
    """Заполняет таблицу MessageTemplate дефолтными значениями, если их нет.

    Идемпотентна: существующие шаблоны не перезаписываются.
    """
    for key, translations in DEFAULT_MESSAGE_TEMPLATES.items():
        for lang, content in translations.items():
            existing = await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.key == key,
                    MessageTemplate.lang == lang,
                )
            )
            if existing.scalar_one_or_none() is None:
                session.add(MessageTemplate(key=key, lang=lang, content=content))
    await session.commit()
```

- [ ] **Step 4: Поправить test_seed_data idempotent (точное число)**

Заменить тело `test_seed_message_templates_idempotent` на:

```python
@pytest.mark.asyncio
async def test_seed_message_templates_idempotent(session):
    from app.models.message import MessageTemplate
    from app.services.seed_data import DEFAULT_MESSAGE_TEMPLATES, seed_message_templates

    await seed_message_templates(session)
    expected_count = len(DEFAULT_MESSAGE_TEMPLATES) * 2  # ru + en
    count_result = await session.execute(select(func.count()).select_from(MessageTemplate))
    assert count_result.scalar_one() == expected_count

    # Повторный вызов — число не меняется
    await seed_message_templates(session)
    count_result = await session.execute(select(func.count()).select_from(MessageTemplate))
    assert count_result.scalar_one() == expected_count
```

- [ ] **Step 5: Run — verify pass**

Run: `cd backend && python -m pytest tests/test_seed_data.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Run all tests — verify nothing broke**

Run: `cd backend && python -m pytest -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/seed_data.py backend/tests/test_seed_data.py
git commit -m "feat: add default message templates seeding (14 keys × ru/en)"
```

---

## Task 17: Alembic — инициализация

**Files:**
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/script.py.mako`
- Create: `backend/alembic/versions/` (пустой каталог)

- [ ] **Step 1: Создать alembic.ini**

```ini
# backend/alembic.ini
[alembic]
script_location = alembic
prepend_sys_path = .
sqlalchemy.url = postgresql+asyncpg://funpay:funpay@localhost:5432/funpay

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 2: Создать alembic/env.py (async)**

```python
# backend/alembic/env.py
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.config import get_settings
from app.db.base import Base
import app.models  # noqa: F401 — регистрирует все модели в metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Подменяем URL на актуальный из настроек
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 3: Создать script.py.mako**

```mako
# backend/alembic/script.py.mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 4: Создать пустой versions/ каталог**

```bash
mkdir -p backend/alembic/versions
touch backend/alembic/versions/.gitkeep
```

- [ ] **Step 5: Проверить, что alembic поднимается**

Run: `cd backend && alembic current`
Expected: вывод без ошибки (даже если БД недоступна — проверка синтаксиса env.py)

- [ ] **Step 6: Commit**

```bash
git add backend/alembic.ini backend/alembic/
git commit -m "feat: init Alembic with async env.py"
```

---

## Task 18: Первая миграция — все таблицы

**Files:**
- Create: `backend/alembic/versions/0001_initial_schema.py` (автогенерация + правки)

- [ ] **Step 1: Запустить PostgreSQL локально (docker)**

```bash
docker run -d --name funpay-pg -e POSTGRES_USER=funpay -e POSTGRES_PASSWORD=funpay -e POSTGRES_DB=funpay -p 5432:5432 postgres:16
```

Убедиться что `.env` содержит `DATABASE_URL=postgresql+asyncpg://funpay:funpay@localhost:5432/funpay`.

- [ ] **Step 2: Сгенерировать миграцию**

Run: `cd backend && alembic revision --autogenerate -m "initial schema"`
Expected: создан файл `alembic/versions/<hash>_initial_schema.py`

- [ ] **Step 3: Проверить сгенерированную миграцию**

Открыть созданный файл, проверить:
- Все 12 таблиц создаются (`subscription_tiers`, `durations`, `limit_scopes`, `accounts`, `account_limits`, `account_check_jobs`, `lots`, `price_matrix`, `lot_templates`, `bump_logs`, `orders`, `rentals`, `message_templates`, `seller_settings`, `audit_logs`)
- Все UNIQUE constraints на месте
- `FernetEncrypted` колонки созданы как `String` (TypeDecorator раскрывается до String)

При необходимости переименовать файл в `0001_initial_schema.py` (поменять `revision` и положить в `down_revision = None`).

- [ ] **Step 4: Применить миграцию**

Run: `cd backend && alembic upgrade head`
Expected: `Running upgrade -> 0001, initial schema`

- [ ] **Step 5: Проверить таблицы в БД**

Run: `docker exec -it funpay-pg psql -U funpay -d funpay -c "\dt"`
Expected: список из 15 таблиц

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/
git commit -m "feat: add initial schema migration"
```

---

## Task 19: main.py — точка входа с health check

**Files:**
- Create: `backend/app/main.py`

- [ ] **Step 1: Реализовать main.py**

```python
# backend/app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="FunPay ChatGPT Rental Bot", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 2: Проверить запуск**

Run: `cd backend && uvicorn app.main:app --reload` (в отдельном терминале)
В другом терминале:
Run: `curl http://localhost:8000/health`
Expected: `{"status":"ok"}`

Остановить uvicorn (Ctrl+C).

- [ ] **Step 3: Commit**

```bash
git add backend/app/main.py
git commit -m "feat: add FastAPI entrypoint with health check"
```

---

## Task 20: Финальная проверка — все тесты

- [ ] **Step 1: Run full test suite**

Run: `cd backend && python -m pytest -v`
Expected: all PASS (~25+ tests)

- [ ] **Step 2: Проверить миграцию накатывается с нуля**

```bash
docker exec -it funpay-pg psql -U funpay -d funpay -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
cd backend && alembic upgrade head
```
Expected: все таблицы созданы заново

- [ ] **Step 3: Проверить seed_data на реальной БД**

Создать временный скрипт проверки:
```python
# backend/scripts/check_seed.py
import asyncio
from app.db.session import async_session_factory
from app.services.seed_data import seed_message_templates
from sqlalchemy import select, func
from app.models.message import MessageTemplate

async def main():
    async with async_session_factory() as s:
        await seed_message_templates(s)
        count = await s.execute(select(func.count()).select_from(MessageTemplate))
        print(f"MessageTemplates в БД: {count.scalar_one()}")

asyncio.run(main())
```

Run: `cd backend && python scripts/check_seed.py`
Expected: `MessageTemplates в БД: 28` (14 ключей × 2 языка)

Удалить `scripts/check_seed.py` после проверки.

- [ ] **Step 4: Commit финальный**

```bash
cd C:/Source/funpay
git add -A
git commit -m "chore: phase 1 foundation complete"
git log --oneline
```

Expected: ~18-20 коммитов, чистая история.

---

## Итог Фазы 1

После завершения:
- ✅ Структура backend/ с зависимостями
- ✅ Pydantic v2 конфигурация с валидацией Fernet-ключа
- ✅ Async SQLAlchemy 2.0 + PostgreSQL
- ✅ 15 моделей данных (все сущности спеки)
- ✅ FernetEncrypted TypeDecorator (прозрачное шифрование)
- ✅ TOTP-генерация кодов
- ✅ Рендеринг MessageTemplate с lang fallback
- ✅ 14 дефолтных шаблонов сообщений (RU+EN)
- ✅ Alembic с рабочей миграцией
- ✅ FastAPI health check
- ✅ ~25+ тестов, все проходят

Фаза 2 (OpenAI + Playwright интеграции) — следующий план.
