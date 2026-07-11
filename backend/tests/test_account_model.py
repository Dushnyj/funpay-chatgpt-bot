import pytest
from sqlalchemy import select

from app.models.account import Account
from app.models.catalog import SubscriptionTier


@pytest.mark.asyncio
async def test_account_password_encrypted_at_rest(session):
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
