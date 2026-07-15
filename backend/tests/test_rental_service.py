import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.funpay_sale import FunPaySale
from app.models.lot import Lot
from app.models.message import MessageTemplate
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings
from app.services.rental_service import RentalService


async def _seed_full(session: AsyncSession):
    tier = SubscriptionTier(
        code="plus", name="Plus", is_active=True, is_sellable=True,
    )
    session.add(tier)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    settings = SellerSettings(id=1)
    session.add(settings)
    await session.flush()
    acc = Account(
        login="acc1",
        password_encrypted="plain_pass",
        totp_secret_encrypted="plain_totp",
        tier_id=tier.id,
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        subscription_expiry_source="accounts_check",
        status="active",
    )
    session.add(acc)
    await session.flush()
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="enc",
        codex_5h_remaining_pct=60,
        codex_weekly_remaining_pct=50,
        codex_primary_remaining_pct=73,
        codex_primary_window_seconds=5 * 60 * 60,
        codex_primary_resets_at=datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc),
        codex_secondary_remaining_pct=61,
        codex_secondary_window_seconds=7 * 86_400,
        codex_secondary_resets_at=datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
        measured_at=datetime.now(timezone.utc),
        refresh_status="ok",
        plan_type="plus",
        plan_window_status="ok",
        expected_long_window_seconds=7 * 24 * 60 * 60,
    )
    session.add(limits)
    lot = Lot(
        funpay_id="5001",
        provenance_token="1" * 32,
        provenance_marker_synced=True,
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        title_ru="Аренда Plus",
        title_en="Plus rental",
        status="active",
    )
    session.add(lot)
    await session.flush()
    order = Order(
        funpay_order_id="ord-1",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        lot_id=lot.id,
        lot_binding_method="offer_id",
        funpay_offer_id=lot.funpay_id,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        status="pending",
    )
    session.add(order)
    await session.flush()
    session.add(FunPaySale(
        funpay_order_id=order.funpay_order_id,
        order_id=order.id,
        funpay_chat_id=order.funpay_chat_id,
        buyer_funpay_id=order.buyer_funpay_id,
        status="paid",
    ))
    await session.flush()
    return tier, duration, scope, acc, order


async def _add_account_with_limits(
    session: AsyncSession,
    tier: SubscriptionTier,
    *,
    login: str,
) -> Account:
    account = Account(
        login=login,
        password_encrypted="plain_pass",
        totp_secret_encrypted="plain_totp",
        tier_id=tier.id,
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=60),
        subscription_expiry_source="accounts_check",
        status="active",
    )
    session.add(account)
    await session.flush()
    session.add(AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="enc",
        codex_primary_remaining_pct=80,
        codex_primary_window_seconds=5 * 60 * 60,
        codex_secondary_remaining_pct=70,
        codex_secondary_window_seconds=7 * 24 * 60 * 60,
        measured_at=datetime.now(timezone.utc),
        refresh_status="ok",
        plan_type="plus",
        plan_window_status="ok",
        expected_long_window_seconds=7 * 24 * 60 * 60,
    ))
    await session.flush()
    return account


async def test_fulfill_order_creates_rental_and_sends_welcome(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()

    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)

    assert rental is not None
    assert rental.account_id == acc.id
    assert rental.order_id == order.id
    assert rental.status == "active"
    assert rental.credentials_delivery_status == "sent"
    assert rental.credentials_delivery_attempts == 1
    assert rental.credentials_delivered_at is not None
    assert rental.issued_codex_primary_pct == 73
    assert rental.issued_codex_primary_window_seconds == 5 * 60 * 60
    assert rental.issued_codex_secondary_pct == 61
    assert rental.issued_codex_secondary_window_seconds == 7 * 24 * 60 * 60
    assert rental.issued_plan_window_status == "ok"
    assert rental.issued_expected_long_window_seconds == 7 * 24 * 60 * 60
    assert rental.issued_limits_measured_at is not None
    assert rental.expires_at > rental.started_at
    assert len(gateway.sent_messages) == 1
    chat_id, text = gateway.sent_messages[0]
    assert chat_id == 100
    assert "61%" in text
    assert "73%" not in text
    assert "7 дн." in text
    assert "20.07.2026 09:00 UTC" in text
    assert "%%" not in text


async def test_capacity_hook_runs_after_commit_before_credential_delivery(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    _tier, _duration, _scope, _account, order = await _seed_full(session)
    events: list[str] = []

    def capacity_changed() -> None:
        # The allocation is already crash-safe before catalog work is queued.
        assert session.in_transaction() is False
        events.append("capacity")

    class OrderedGateway(FakeChatGateway):
        async def send_message(self, chat_id: int, text: str) -> int:
            assert events == ["capacity"]
            events.append("delivery")
            return await super().send_message(chat_id, text)

    rental = await RentalService(
        capacity_changed=capacity_changed,
    ).fulfill_order(
        session,
        OrderedGateway(),
        order.id,
        default_max_active_rentals=1,
    )

    assert rental is not None
    assert events == ["capacity", "delivery"]


async def test_fulfill_order_sends_no_account_message_when_pool_empty(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    acc.status = "maintenance"
    await session.flush()
    gateway = FakeChatGateway()
    capacity_events: list[str] = []
    svc = RentalService(capacity_changed=lambda: capacity_events.append("reconcile"))

    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)

    assert rental is None
    assert len(gateway.sent_messages) == 1
    assert capacity_events == ["reconcile"]


async def test_no_account_message_timeout_releases_order_transaction(
    session: AsyncSession,
    monkeypatch,
):
    import app.services.rental_service as rental_module
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    _tier, _duration, _scope, account, order = await _seed_full(session)
    account.status = "maintenance"
    await session.flush()
    monkeypatch.setattr(
        rental_module, "CREDENTIAL_SEND_TIMEOUT_SECONDS", 0.01,
    )

    class SlowGateway(FakeChatGateway):
        async def send_message(self, chat_id: int, text: str) -> int:
            await asyncio.sleep(60)
            return await super().send_message(chat_id, text)

    with pytest.raises(TimeoutError):
        await RentalService().fulfill_order(
            session,
            SlowGateway(),
            order.id,
            default_max_active_rentals=1,
        )

    assert session.in_transaction() is False


async def test_scheduled_retry_can_suppress_repeated_no_account_message(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    _tier, duration, _scope, account, order = await _seed_full(session)
    duration.minutes = 30
    duration.sort_order = 30
    account.status = "maintenance"
    await session.flush()
    gateway = FakeChatGateway()

    rental = await RentalService().fulfill_order(
        session,
        gateway,
        order.id,
        default_max_active_rentals=1,
        notify_unavailable=False,
    )

    assert rental is None
    assert gateway.sent_messages == []


async def test_fulfill_order_refuses_non_pending_order(session: AsyncSession):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    _tier, _duration, _scope, _account, order = await _seed_full(session)
    order.status = "refunded"
    await session.flush()
    gateway = FakeChatGateway()

    rental = await RentalService().fulfill_order(
        session, gateway, order.id, default_max_active_rentals=1
    )

    assert rental is None
    assert gateway.sent_messages == []


async def test_fulfill_order_idempotent_existing_rental(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()
    first = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    gateway.sent_messages.clear()
    second = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    assert second is not None
    assert second.id == first.id
    assert len(gateway.sent_messages) == 0

    rentals = (await session.execute(select(Rental).where(Rental.order_id == order.id))).scalars().all()
    assert len(rentals) == 1


async def test_failed_delivery_is_durable_and_retry_reuses_same_rental(
    session: AsyncSession,
    test_engine,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    _tier, duration, _scope, account, order = await _seed_full(session)
    duration.minutes = 30
    duration.sort_order = 30
    gateway = FakeChatGateway()
    gateway.send_message = AsyncMock(side_effect=RuntimeError("ambiguous transport"))
    service = RentalService()

    with pytest.raises(RuntimeError, match="ambiguous transport"):
        await service.fulfill_order(
            session, gateway, order.id, default_max_active_rentals=1
        )

    rentals = (
        await session.execute(select(Rental).where(Rental.order_id == order.id))
    ).scalars().all()
    assert len(rentals) == 1
    failed = rentals[0]
    assert failed.account_id == account.id
    assert failed.credentials_delivery_status == "failed"
    assert failed.credentials_delivery_attempts == 1
    assert failed.credentials_delivery_last_error == "delivery_failed:RuntimeError"
    assert failed.credentials_delivery_next_attempt_at is not None

    # A later live usage refresh must become the final durable issuance: the
    # buyer message and admin snapshot describe the same eligibility sample.
    # Simulate a scheduler refresh in another session. The delivery session
    # still has the old identity-map object and must explicitly refresh it.
    updater_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with updater_factory() as updater:
        limits = await updater.get(AccountLimits, account.id)
        assert limits is not None
        limits.codex_primary_remaining_pct = 1
        limits.codex_secondary_resets_at = datetime(
            2026, 8, 12, 9, 0, tzinfo=timezone.utc
        )
        limits.codex_secondary_remaining_pct = 1
        limits.measured_at = datetime.now(timezone.utc)
        await updater.commit()
    gateway.send_message = AsyncMock(return_value=None)
    failed.credentials_delivery_next_attempt_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    await session.commit()
    retried = await service.fulfill_order(
        session, gateway, order.id, default_max_active_rentals=1
    )

    assert retried is not None
    assert retried.id == failed.id
    assert retried.account_id == account.id
    assert retried.credentials_delivery_status == "sent"
    assert retried.credentials_delivery_attempts == 2
    delivered_text = gateway.send_message.await_args.kwargs["text"]
    assert "1%" in delivered_text
    assert "12.08.2026 09:00 UTC" in delivered_text
    assert retried.issued_codex_primary_pct == 1
    assert retried.issued_codex_secondary_pct == 1
    assert retried.issued_codex_secondary_window_seconds == 7 * 24 * 60 * 60
    assert retried.credentials_delivered_at is not None
    term = (
        retried.expires_at.replace(tzinfo=timezone.utc)
        - retried.credentials_delivered_at.replace(tzinfo=timezone.utc)
    )
    assert timedelta(minutes=29, seconds=59) <= term <= timedelta(minutes=30, seconds=1)
    rentals = (
        await session.execute(select(Rental).where(Rental.order_id == order.id))
    ).scalars().all()
    assert len(rentals) == 1


async def test_rental_allocation_is_committed_before_credentials_are_sent(
    session: AsyncSession,
    test_engine,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    _tier, _duration, _scope, _account, order = await _seed_full(session)
    inspector_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    observed: dict[str, object] = {}

    class InspectingGateway(FakeChatGateway):
        async def send_message(self, chat_id: int, text: str) -> None:
            async with inspector_factory() as inspector:
                persisted = (
                    await inspector.execute(
                        select(Rental).where(Rental.order_id == order.id)
                    )
                ).scalar_one()
                observed["id"] = persisted.id
                observed["status"] = persisted.credentials_delivery_status
                observed["attempts"] = persisted.credentials_delivery_attempts
            await super().send_message(chat_id, text)

    rental = await RentalService().fulfill_order(
        session,
        InspectingGateway(),
        order.id,
        default_max_active_rentals=1,
    )

    assert rental is not None
    assert observed == {
        "id": rental.id,
        "status": "sending",
        "attempts": 1,
    }
    assert rental.credentials_delivery_status == "sent"


async def test_recent_sending_delivery_is_not_duplicated_but_stale_is_reclaimed(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    _tier, _duration, _scope, _account, order = await _seed_full(session)
    gateway = FakeChatGateway()
    gateway.send_message = AsyncMock(side_effect=RuntimeError("first failure"))
    service = RentalService()
    with pytest.raises(RuntimeError):
        await service.fulfill_order(
            session, gateway, order.id, default_max_active_rentals=1
        )
    rental = (
        await session.execute(select(Rental).where(Rental.order_id == order.id))
    ).scalar_one()
    rental.credentials_delivery_status = "sending"
    rental.credentials_delivery_started_at = datetime.now(timezone.utc)
    rental.credentials_delivery_next_attempt_at = None
    rental.credentials_delivery_last_error = None
    await session.commit()

    gateway.send_message = AsyncMock(return_value=None)
    recent = await service.fulfill_order(
        session, gateway, order.id, default_max_active_rentals=1
    )
    assert recent is not None and recent.id == rental.id
    gateway.send_message.assert_not_awaited()
    assert recent.credentials_delivery_attempts == 1

    rental.credentials_delivery_started_at = (
        datetime.now(timezone.utc) - timedelta(minutes=6)
    )
    rental.credentials_delivery_next_attempt_at = None
    await session.commit()
    stale = await service.fulfill_order(
        session, gateway, order.id, default_max_active_rentals=1
    )
    assert stale is not None and stale.id == rental.id
    gateway.send_message.assert_awaited_once()
    assert stale.credentials_delivery_status == "sent"
    assert stale.credentials_delivery_attempts == 2


async def test_fulfill_order_records_issued_limits(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()
    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    assert rental is not None
    assert rental.issued_codex_5h_pct == 60
    assert rental.issued_codex_weekly_pct == 50
    assert rental.issued_codex_primary_pct == 73
    assert rental.issued_codex_primary_window_seconds == 5 * 60 * 60
    assert rental.issued_codex_primary_resets_at is not None
    assert rental.issued_codex_primary_resets_at.replace(
        tzinfo=timezone.utc
    ) == datetime(
        2026, 7, 13, 14, 0, tzinfo=timezone.utc
    )
    assert rental.issued_codex_secondary_pct == 61
    assert rental.issued_codex_secondary_window_seconds == 7 * 86_400
    assert rental.issued_codex_secondary_resets_at is not None
    assert rental.issued_codex_secondary_resets_at.replace(
        tzinfo=timezone.utc
    ) == datetime(
        2026, 7, 20, 9, 0, tzinfo=timezone.utc
    )
    assert rental.issued_plan_window_status == "ok"
    assert rental.issued_expected_long_window_seconds == 7 * 86_400
    assert rental.issued_limits_measured_at is not None


async def test_revoke_rental_sets_status(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()
    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    assert rental is not None
    await svc.revoke_rental(session, rental.id)
    await session.refresh(rental)
    assert rental.status == "revoked"


async def test_confirmed_welcome_starts_full_term_after_slow_send(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    _tier, duration, _scope, _account, order = await _seed_full(session)
    duration.minutes = 30
    duration.sort_order = 30

    class SlowGateway(FakeChatGateway):
        ack_at: datetime | None = None

        async def send_message(self, chat_id: int, text: str) -> int:
            await asyncio.sleep(0.02)
            result = await super().send_message(chat_id, text)
            self.ack_at = datetime.now(timezone.utc)
            return result

    gateway = SlowGateway()
    rental = await RentalService().fulfill_order(session, gateway, order.id, 1)

    assert rental is not None and gateway.ack_at is not None
    delivered_at = rental.credentials_delivered_at.replace(tzinfo=timezone.utc)
    assert delivered_at >= gateway.ack_at - timedelta(milliseconds=5)
    assert rental.expires_at.replace(tzinfo=timezone.utc) - delivered_at == (
        timedelta(minutes=30)
    )


async def test_ambiguous_welcome_retry_never_reallocates_second_account(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    tier, duration, _scope, first, order = await _seed_full(session)
    duration.minutes = 30
    duration.sort_order = 30
    second = await _add_account_with_limits(
        session, tier, login="never-disclose-second",
    )

    class AcceptedThenRaised(FakeChatGateway):
        async def send_message(self, chat_id: int, text: str) -> int:
            await super().send_message(chat_id, text)
            raise TimeoutError("ack lost")

    service = RentalService()
    first_gateway = AcceptedThenRaised()
    with pytest.raises(TimeoutError, match="ack lost"):
        await service.fulfill_order(session, first_gateway, order.id, 1)

    rental = (
        await session.execute(select(Rental).where(Rental.order_id == order.id))
    ).scalar_one()
    first_deadline = rental.expires_at
    assert rental.account_id == first.id
    assert rental.credentials_delivery_attempts == 1
    assert len(first_gateway.sent_messages) == 1

    first.status = "maintenance"
    rental.credentials_delivery_next_attempt_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    await session.commit()
    retry_gateway = FakeChatGateway()
    retried = await service.fulfill_order(session, retry_gateway, order.id, 1)

    assert retried is not None
    assert retried.credentials_delivery_status == "manual"
    assert retried.account_id == first.id
    assert retried.account_id != second.id
    assert retried.expires_at == first_deadline
    assert retry_gateway.sent_messages == []


async def test_reallocation_rechecks_refund_after_commit_before_send(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    tier, duration, _scope, first, order = await _seed_full(session)
    second = await _add_account_with_limits(
        session, tier, login="refund-race-second",
    )

    class RefundDuringReallocationPool:
        async def acquire(self, *_args, **_kwargs):
            first.status = "maintenance"
            return first

        async def acquire_excluding(self, *_args, **_kwargs):
            order.status = "refunded"
            return second

    gateway = FakeChatGateway()
    rental = await RentalService(
        account_pool=RefundDuringReallocationPool()
    ).fulfill_order(session, gateway, order.id, 1)

    assert rental is not None
    assert rental.account_id == second.id
    assert rental.credentials_delivery_status == "failed"
    assert gateway.sent_messages == []


@pytest.mark.parametrize("catalog_switch", ["tier", "scope"])
async def test_delivery_retry_honours_disabled_catalog_switch(
    session: AsyncSession,
    catalog_switch: str,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    tier, _duration, scope, _account, order = await _seed_full(session)
    service = RentalService()
    failing = FakeChatGateway()
    failing.send_message = AsyncMock(side_effect=RuntimeError("lost ack"))
    with pytest.raises(RuntimeError, match="lost ack"):
        await service.fulfill_order(session, failing, order.id, 1)
    rental = (
        await session.execute(select(Rental).where(Rental.order_id == order.id))
    ).scalar_one()
    if catalog_switch == "tier":
        tier.is_sellable = False
    else:
        scope.is_enabled = False
    rental.credentials_delivery_next_attempt_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    await session.commit()

    retry_gateway = FakeChatGateway()
    retried = await service.fulfill_order(session, retry_gateway, order.id, 1)

    assert retried is not None
    assert retried.credentials_delivery_status == "manual"
    assert retry_gateway.sent_messages == []


async def test_welcome_retry_uses_same_remaining_term_for_all_duration_fields(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    _tier, _duration, _scope, _account, order = await _seed_full(session)
    service = RentalService()
    failing = FakeChatGateway()
    failing.send_message = AsyncMock(side_effect=RuntimeError("lost ack"))
    with pytest.raises(RuntimeError):
        await service.fulfill_order(session, failing, order.id, 1)
    rental = (
        await session.execute(select(Rental).where(Rental.order_id == order.id))
    ).scalar_one()
    rental.expires_at = datetime.now(timezone.utc) + timedelta(minutes=150)
    rental.credentials_delivery_next_attempt_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    template = (
        await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.key == "welcome",
                MessageTemplate.lang == "ru",
            )
        )
    ).scalar_one()
    template.content = (
        "{login}|{password}|D={duration}|M={duration_minutes}|X={days}"
    )
    await session.commit()

    gateway = FakeChatGateway()
    await service.fulfill_order(session, gateway, order.id, 1)

    text = gateway.sent_messages[0][1]
    assert "D=2 ч 30 мин" in text
    assert "M=150" in text
    assert "X=0.1041666667" in text
