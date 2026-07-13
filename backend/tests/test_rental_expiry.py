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
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
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
        credentials_delivery_status="sent",
        credentials_delivery_template="welcome",
        expiry_notified_at=(
            datetime.now(timezone.utc) if status == "expired" else None
        ),
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


async def test_expiry_releases_stale_replacement_target(
    session: AsyncSession,
):
    rental = await _make_rental(
        session, expires_delta=timedelta(seconds=-1),
    )
    target = Account(
        login="stale-expiry-target",
        password_encrypted="enc",
        totp_secret_encrypted="enc",
        tier_id=rental.tier_id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(target)
    await session.flush()
    rental.replacement_target_account_id = target.id
    rental.expiry_revoke_started_at = (
        datetime.now(timezone.utc) - timedelta(minutes=6)
    )
    await session.commit()

    await _service().expire_overdue(session, gateway=None)

    await session.refresh(rental)
    assert rental.status == "expired"
    assert rental.replacement_target_account_id is None


@pytest.mark.parametrize(
    ("claim_age", "released"),
    [
        (timedelta(minutes=6), True),
        (timedelta(minutes=1), False),
    ],
)
async def test_periodic_cleanup_releases_only_stale_exact_replacement_claim(
    session: AsyncSession,
    claim_age: timedelta,
    released: bool,
):
    rental = await _make_rental(
        session, expires_delta=timedelta(days=1),
    )
    target = Account(
        login="periodic-cleanup-target",
        password_encrypted="enc",
        totp_secret_encrypted="enc",
        tier_id=rental.tier_id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(target)
    await session.flush()
    rental.replacement_target_account_id = target.id
    rental.expiry_revoke_started_at = datetime.now(timezone.utc) - claim_age
    old_account = await session.get(Account, rental.account_id)
    old_account.status = "maintenance"
    await session.commit()

    processed = await _service().expire_overdue(session, gateway=None)

    await session.refresh(rental)
    await session.refresh(old_account)
    assert processed == []
    assert (rental.replacement_target_account_id is None) is released
    assert (rental.expiry_revoke_started_at is None) is released
    assert old_account.status == "maintenance"


async def test_expire_skips_active_rentals(session: AsyncSession):
    await _make_rental(session, expires_delta=timedelta(days=1))
    gateway = FakeChatGateway()
    svc = _service()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 0
    assert len(gateway.sent_messages) == 0


async def test_expire_skips_unsent_initial_rental_provisional_deadline(
    session: AsyncSession,
):
    rental = await _make_rental(
        session, expires_delta=timedelta(minutes=-5)
    )
    rental.credentials_delivery_status = "failed"
    await session.flush()

    expired = await _service().expire_overdue(session, gateway=None)

    assert expired == []
    await session.refresh(rental)
    assert rental.status == "active"


async def test_expire_skips_already_expired(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _make_rental(session, expires_delta=timedelta(seconds=-1), status="expired")
    gateway = FakeChatGateway()
    svc = _service()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 0
    assert len(gateway.sent_messages) == 0


@pytest.mark.parametrize("order_status", ["refund_pending", "refunded"])
async def test_expire_never_races_refund_owned_rental(
    session: AsyncSession,
    order_status: str,
):
    rental = await _make_rental(
        session,
        expires_delta=timedelta(seconds=-1),
    )
    order = await session.get(Order, rental.order_id)
    assert order is not None
    order.status = order_status
    await session.flush()
    kick = FakeKickService()

    expired = await RentalExpiryService(kick_service=kick).expire_overdue(
        session,
        FakeChatGateway(),
    )

    await session.refresh(rental)
    assert expired == []
    assert rental.status == "active"
    assert kick.account_ids == []


async def test_refund_claim_between_expiry_claim_and_finalize_owns_rental(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _make_rental(
        session,
        expires_delta=timedelta(seconds=-1),
    )
    order = await session.get(Order, rental.order_id)
    assert order is not None

    class RefundDuringKick:
        async def kick(self, db, _account_id: int) -> KickResult:
            current_order = await db.get(Order, order.id)
            assert current_order is not None
            current_order.status = "refund_pending"
            await db.commit()
            return KickResult(success=True)

    gateway = FakeChatGateway()
    processed = await RentalExpiryService(
        kick_service=RefundDuringKick(),
    ).expire_overdue(session, gateway)

    await session.refresh(order)
    await session.refresh(rental)
    assert len(processed) == 1
    assert order.status == "refund_pending"
    assert rental.status == "expiry_pending"
    assert rental.expiry_revoke_started_at is None
    assert gateway.sent_messages == []


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


async def test_expiry_notification_survives_crash_after_revoke_finalize(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _make_rental(
        session, expires_delta=timedelta(seconds=-1)
    )
    service = _service()

    # Simulate a process dying after the durable revoke finalization and before
    # it had a gateway capable of sending the terminal buyer message.
    await service.expire_overdue(session, gateway=None)
    await session.refresh(rental)
    assert rental.status == "expired"
    assert rental.expiry_notified_at is None

    gateway = FakeChatGateway()
    processed = await service.expire_overdue(session, gateway)

    assert processed == []
    assert len(gateway.sent_messages) == 1
    await session.refresh(rental)
    assert rental.expiry_notified_at is not None


async def test_expiry_notification_send_is_bounded_and_retryable(
    session: AsyncSession,
    monkeypatch,
):
    import asyncio
    import app.services.rental_expiry as expiry_module
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _make_rental(
        session, expires_delta=timedelta(seconds=-1)
    )
    monkeypatch.setattr(
        expiry_module, "EXPIRY_MESSAGE_TIMEOUT_SECONDS", 0.01,
    )

    class SlowGateway(FakeChatGateway):
        async def send_message(self, chat_id: int, text: str) -> int:
            await asyncio.sleep(60)
            return await super().send_message(chat_id, text)

    await _service().expire_overdue(session, SlowGateway())

    await session.refresh(rental)
    assert rental.status == "expired"
    assert rental.expiry_notified_at is None
    assert rental.expiry_revoke_started_at is None

    gateway = FakeChatGateway()
    await _service().expire_overdue(session, gateway)
    assert len(gateway.sent_messages) == 1
    await session.refresh(rental)
    assert rental.expiry_notified_at is not None
