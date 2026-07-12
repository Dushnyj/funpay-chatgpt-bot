import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.types.encrypted import FernetEncrypted
from app.services.crypto import encrypt


class _SampleModel(Base):
    __tablename__ = "sample_for_encrypted_test"

    id: Mapped[int] = mapped_column(primary_key=True)
    secret: Mapped[str] = mapped_column(FernetEncrypted)


@pytest.mark.asyncio
async def test_encrypted_type_roundtrips(session):
    obj = _SampleModel(secret="my-plaintext-secret")
    session.add(obj)
    await session.commit()

    fetched = await session.execute(select(_SampleModel).where(_SampleModel.id == obj.id))
    reloaded = fetched.scalar_one()
    assert reloaded.secret == "my-plaintext-secret"


@pytest.mark.asyncio
async def test_encrypted_type_stores_ciphertext_not_plaintext(session):
    obj = _SampleModel(secret="plaintext-value")
    session.add(obj)
    await session.commit()

    # Читаем raw-значение из БД, минуя TypeDecorator
    raw = await session.execute(text("SELECT secret FROM sample_for_encrypted_test"))
    raw_value = raw.scalar_one()
    assert raw_value != "plaintext-value"


@pytest.mark.asyncio
async def test_encrypted_type_reads_historical_double_encryption(session):
    double_encrypted = encrypt(encrypt("legacy-secret"))
    await session.execute(
        text("INSERT INTO sample_for_encrypted_test (id, secret) VALUES (99, :secret)"),
        {"secret": double_encrypted},
    )
    await session.commit()

    fetched = await session.get(_SampleModel, 99)
    assert fetched is not None
    assert fetched.secret == "legacy-secret"
