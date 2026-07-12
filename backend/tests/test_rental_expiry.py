from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Order, Rental
from app.models.audit import AuditLog
from app.models.account import AccountCheckJob
from app.services.kick_service import KickResult
from app.services.rental_expiry import RentalExpiryService


class FakeKickService:
    def __init__(self, result: KickResult | None = None):
        self.result = result or KickResult(success=True)
        self.account_ids: list[int] = []

    async def kick(self, _session, account_id: int) -> KickResult:
        self.account_ids.append(account_id)
        return self.result


def _service(result: KickResult | None = None) -> RentalExpiryService:
    return RentalExpiryService(kick_service=FakeKickService(result))


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
    await session.flush()
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
    svc = _service()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 1
    await session.refresh(rental)
    assert rental.status == "expired"
    assert len(gateway.sent_messages) == 1


async def test_expire_skips_active_rentals(session: AsyncSession):
    await _make_rental(session, expires_delta=timedelta(days=1))
    gateway = FakeChatGateway()
    svc = _service()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 0
    assert len(gateway.sent_messages) == 0


async def test_expire_skips_already_expired(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _make_rental(session, expires_delta=timedelta(seconds=-1), status="expired")
    gateway = FakeChatGateway()
    svc = _service()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 0
    assert len(gateway.sent_messages) == 0


async def test_expire_sends_to_correct_chat(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _make_rental(session, expires_delta=timedelta(seconds=-1), chat_id="555")
    gateway = FakeChatGateway()
    svc = _service()
    await svc.expire_overdue(session, gateway)
    assert len(gateway.sent_messages) == 1
    chat_id, _ = gateway.sent_messages[0]
    assert chat_id == 555


async def test_expiry_kicks_account_audits_and_enqueues_recovery(session: AsyncSession):
    from sqlalchemy import select
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _make_rental(session, expires_delta=timedelta(seconds=-1))
    kick = FakeKickService()
    await RentalExpiryService(kick_service=kick).expire_overdue(
        session, FakeChatGateway(),
    )

    assert kick.account_ids == [rental.account_id]
    audit = (await session.execute(
        select(AuditLog).where(AuditLog.event_type == "rental_expiry_kick")
    )).scalar_one()
    assert audit.metadata_["success"] is True
    job = (await session.execute(
        select(AccountCheckJob).where(AccountCheckJob.account_id == rental.account_id)
    )).scalar_one()
    assert job.job_type == "refresh_recover"


async def test_expiry_failure_is_explicit_and_retryable(session: AsyncSession):
    rental = await _make_rental(session, expires_delta=timedelta(seconds=-1))
    service = _service(KickResult(success=False, error="browser failed"))

    await service.expire_overdue(session, gateway=None)

    await session.refresh(rental)
    assert rental.status == "expiry_pending"

    service._kick.result = KickResult(success=True)
    await service.expire_overdue(session, gateway=None)
    await session.refresh(rental)
    assert rental.status == "expired"


async def test_expiry_notifies_other_active_renters_after_shared_account_kick(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    overdue = await _make_rental(session, expires_delta=timedelta(seconds=-1))
    other_order = Order(
        funpay_order_id="o2", funpay_chat_id="777", buyer_funpay_id="201",
        tier_id=overdue.tier_id, duration_id=overdue.duration_id,
        limit_scope_id=overdue.limit_scope_id, price=100, status="pending",
    )
    session.add(other_order)
    await session.flush()
    session.add(Rental(
        order_id=other_order.id,
        account_id=overdue.account_id,
        buyer_funpay_id="201",
        buyer_funpay_chat_id="777",
        tier_id=overdue.tier_id,
        duration_id=overdue.duration_id,
        limit_scope_id=overdue.limit_scope_id,
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        status="active",
    ))
    await session.flush()
    gateway = FakeChatGateway()

    await _service().expire_overdue(session, gateway)

    assert {chat_id for chat_id, _ in gateway.sent_messages} == {100, 777}
