import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.message import MessageTemplate


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_update_and_list_templates(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.put("/api/templates", json={
        "items": [
            {
                "key": "welcome",
                "lang": "ru",
                "content": "Логин: {login}; пароль: {password}",
            },
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1

    resp = await auth_client.get("/api/templates")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["key"] == "welcome"


async def test_update_template_idempotent(auth_client: AsyncClient, session: AsyncSession):
    await auth_client.put("/api/templates", json={
        "items": [{"key": "help", "lang": "ru", "content": "Старый"}],
    })
    resp = await auth_client.put("/api/templates", json={
        "items": [{"key": "help", "lang": "ru", "content": "Новый"}],
    })
    assert resp.status_code == 200

    resp = await auth_client.get("/api/templates")
    items = resp.json()
    assert len(items) == 1
    assert items[0]["content"] == "Новый"


@pytest.mark.parametrize(
    "content",
    [
        "Привет, {not_allowed}",
        "Привет, {login.__class__}",
        "Привет, {login",
    ],
)
async def test_update_rejects_unknown_unsafe_and_broken_placeholders(
    auth_client: AsyncClient,
    content: str,
):
    resp = await auth_client.put(
        "/api/templates",
        json={
            "items": [{"key": "welcome", "lang": "ru", "content": content}]
        },
    )

    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


async def test_update_rejects_unknown_template_key(auth_client: AsyncClient):
    resp = await auth_client.put(
        "/api/templates",
        json={
            "items": [{"key": "custom", "lang": "ru", "content": "Текст"}]
        },
    )

    assert resp.status_code == 422
    assert "Unknown template key" in resp.json()["detail"]


async def test_invalid_batch_does_not_modify_valid_template(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    template = MessageTemplate(key="help", lang="ru", content="Исходный текст")
    session.add(template)
    await session.commit()

    resp = await auth_client.put(
        "/api/templates",
        json={
            "items": [
                {"key": "help", "lang": "ru", "content": "Изменённый текст"},
                {
                    "key": "welcome",
                    "lang": "ru",
                    "content": "Опасный {login.password}",
                },
            ]
        },
    )

    assert resp.status_code == 422
    await session.refresh(template)
    assert template.content == "Исходный текст"


async def test_update_rejects_duplicate_template_identity(auth_client: AsyncClient):
    resp = await auth_client.put(
        "/api/templates",
        json={
            "items": [
                {"key": "help", "lang": "ru", "content": "Первый"},
                {"key": "help", "lang": "ru", "content": "Второй"},
            ]
        },
    )

    assert resp.status_code == 422
    assert "Duplicate template" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("key", "content"),
    [
        ("welcome", "Только логин: {login}"),
        ("replace_success", "Новый пароль: {password}"),
        ("code_success", "Код подготовлен"),
    ],
)
async def test_update_rejects_template_without_required_secret_fields(
    auth_client: AsyncClient,
    key: str,
    content: str,
):
    response = await auth_client.put(
        "/api/templates",
        json={"items": [{"key": key, "lang": "ru", "content": content}]},
    )

    assert response.status_code == 422
    assert "must include" in response.json()["detail"]


async def test_update_rejects_content_larger_than_funpay_limit(
    auth_client: AsyncClient,
):
    response = await auth_client.put(
        "/api/templates",
        json={
            "items": [
                {
                    "key": "help",
                    "lang": "ru",
                    "content": "x" * 4_001,
                }
            ]
        },
    )

    assert response.status_code == 422
