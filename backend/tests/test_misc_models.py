import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

# Регистрируем модели в Base.metadata на этапе импорта модуля,
# чтобы create_all в фикстуре test_engine создал все таблицы.
from app.models.audit import AuditLog
from app.models.message import MessageTemplate
from app.models.settings import SellerSettings


@pytest.mark.asyncio
async def test_message_template_unique_per_key_lang(session):
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
    entry = AuditLog(
        event_type="rental_created",
        message_text="Создана аренда #1",
        metadata_={"rental_id": 1, "account_id": 2},
    )
    session.add(entry)
    await session.commit()

    fetched = await session.execute(select(AuditLog).where(AuditLog.event_type == "rental_created"))
    reloaded = fetched.scalar_one()
    assert reloaded.metadata_["rental_id"] == 1
    assert reloaded.timestamp is not None
