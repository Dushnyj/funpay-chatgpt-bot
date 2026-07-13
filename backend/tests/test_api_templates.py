import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.catalog import LimitScope, SubscriptionTier
from app.models.message import MessageTemplate
from app.services.seed_data import seed_lot_templates


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
    assert {"login", "password"} <= set(items[0]["allowed_fields"])
    assert items[0]["is_custom"] is True


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


async def test_reset_message_template_restores_bundled_default(
    auth_client: AsyncClient,
):
    await auth_client.put(
        "/api/templates",
        json={"items": [{"key": "help", "lang": "ru", "content": "Свой"}]},
    )

    response = await auth_client.post("/api/templates/messages/help/ru/reset")

    assert response.status_code == 200
    assert response.json()["is_custom"] is False
    assert response.json()["content"] == response.json()["default_content"]


async def test_lot_template_crud_validation_and_system_reset(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    await seed_lot_templates(session)
    tier = SubscriptionTier(name="Custom", is_active=True)
    scope = LimitScope(code="custom", name="Custom")
    session.add_all([tier, scope])
    await session.commit()

    listed = await auth_client.get("/api/templates/lot")
    assert listed.status_code == 200
    default = listed.json()[0]
    assert default["key"] == "default"
    assert default["system_managed"] is True
    assert {"plan", "days", "condition"} <= set(default["allowed_fields"])

    payload = {
        "key": "custom-plus",
        "name": "Custom Plus",
        "tier_id": tier.id,
        "limit_scope_id": scope.id,
        "title_ru": "{plan} / {days} / {condition}",
        "title_en": "{plan} / {days} / {condition}",
        "description_ru": "Окно {long_window_days}",
        "description_en": "Window {long_window_days}",
        "enabled": True,
    }
    created = await auth_client.post("/api/templates/lot", json=payload)
    assert created.status_code == 201
    assert created.json()["tier_id"] == tier.id
    assert created.json()["is_custom"] is True
    duplicate_target = await auth_client.post(
        "/api/templates/lot", json={**payload, "key": "another-key"}
    )
    assert duplicate_target.status_code == 409

    invalid = await auth_client.put(
        "/api/templates/lot/custom-plus",
        json={
            **{
                key: payload[key]
                for key in (
                    "title_ru", "title_en", "description_ru", "description_en"
                )
            },
            "title_ru": "Опасно {plan.__class__}",
            "enabled": True,
        },
    )
    assert invalid.status_code == 422

    deleted = await auth_client.delete("/api/templates/lot/custom-plus")
    assert deleted.status_code == 204
    protected = await auth_client.delete("/api/templates/lot/default")
    assert protected.status_code == 409

    updated = await auth_client.put(
        "/api/templates/lot/default",
        json={
            "title_ru": "{plan} {days} {condition}",
            "title_en": "{plan} {days} {condition}",
            "description_ru": "Свой текст",
            "description_en": "Custom text",
            "enabled": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["is_custom"] is True
    disabled_system = await auth_client.put(
        "/api/templates/lot/default",
        json={**updated.json(), "enabled": False},
    )
    assert disabled_system.status_code == 422
    reset = await auth_client.post("/api/templates/lot/default/reset")
    assert reset.status_code == 200
    assert reset.json()["enabled"] is True
    assert reset.json()["is_custom"] is False


async def test_lot_template_rejects_unknown_target(
    auth_client: AsyncClient,
):
    payload = {
        "key": "bad-target",
        "name": "Bad target",
        "tier_id": 999,
        "limit_scope_id": None,
        "title_ru": "{plan} {days} {condition}",
        "title_en": "{plan} {days} {condition}",
        "description_ru": "",
        "description_en": "",
        "enabled": True,
    }
    response = await auth_client.post("/api/templates/lot", json=payload)
    assert response.status_code == 422


async def test_lot_template_rejects_whitespace_only_name(
    auth_client: AsyncClient,
):
    response = await auth_client.post(
        "/api/templates/lot",
        json={
            "key": "blank-name",
            "name": "   ",
            "tier_id": None,
            "limit_scope_id": None,
            "title_ru": "{plan} {days} {condition}",
            "title_en": "{plan} {days} {condition}",
            "description_ru": "",
            "description_en": "",
            "enabled": False,
        },
    )

    assert response.status_code == 422


async def test_lot_template_api_requires_admin_session():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/templates/lot")

    assert response.status_code == 401
