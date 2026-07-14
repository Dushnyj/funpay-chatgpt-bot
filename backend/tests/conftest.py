from collections.abc import AsyncGenerator
import os

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
_COLLECTION_ENV = {
    "DATABASE_URL": TEST_DATABASE_URL,
    "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "SECRET_KEY": "test-secret-key-at-least-32-bytes-long",
    "ADMIN_PASSWORD_HASH": "$2b$12$dummyhash",
    "ADMIN_COOKIE_SECURE": "true",
    "FUNPAY_SESSION_KEY": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_SELLER_CHAT_ID": "",
    "MICROSOFT_GRAPH_CLIENT_ID": "",
    "MICROSOFT_GRAPH_CLIENT_SECRET": "",
    "MICROSOFT_GRAPH_REDIRECT_URI": "",
}
for _name, _value in _COLLECTION_ENV.items():
    os.environ[_name] = _value

# Import application metadata only after collection itself has been isolated
# from a developer's local .env. Fixtures run too late for test-module imports.
from app.db.base import Base  # noqa: E402


def _gen_fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    """Изолирует тесты от реального .env — единая точка настройки окружения."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("ENCRYPTION_KEY", _gen_fernet_key())
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-at-least-32-bytes-long")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$dummyhash")
    monkeypatch.setenv("ADMIN_COOKIE_SECURE", "true")
    monkeypatch.setenv("FUNPAY_SESSION_KEY", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "")
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_ID", "")
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_SECRET", "")
    monkeypatch.setenv("MICROSOFT_GRAPH_REDIRECT_URI", "")

    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest_asyncio.fixture(autouse=True)
async def _override_app_db(session, _test_env):
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
