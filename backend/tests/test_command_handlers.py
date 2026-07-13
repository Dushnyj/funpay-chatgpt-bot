from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.email.provider import (
    EmailErrorCode,
    EmailProviderError,
    FreshVerificationCode,
)
from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account, AccountLimits
from app.models.audit import AuditLog
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Order, Rental
from app.services.command_handlers import (
    CodeHandler,
    HelpHandler,
    SellerHandler,
    SubscriptionHandler,
    _wait_for_safe_totp_window,
)
from app.services.command_router import CommandContext
from app.services.command_parser import CommandType, ParsedCommand


async def _seed_rental(session: AsyncSession, chat_id: int = 100) -> Rental:
    tier = SubscriptionTier(
        code="plus", name="Plus", is_active=True, is_sellable=True,
    )
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    acc = Account(
        login="acc1",
        password_encrypted="enc",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    session.add(AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="enc",
        chat_5h_remaining_pct=80,
        chat_weekly_remaining_pct=70,
        codex_5h_remaining_pct=60,
        codex_weekly_remaining_pct=50,
        measured_at=datetime.now(timezone.utc),
        refresh_status="ok",
    ))
    order = Order(
        funpay_order_id="o1",
        funpay_chat_id=str(chat_id),
        buyer_funpay_id="200",
        lot_id=None, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=100, status="pending",
    )
    session.add(order)
    await session.flush()
    rental = Rental(
        order_id=order.id, account_id=acc.id,
        buyer_funpay_id="200", buyer_funpay_chat_id=str(chat_id),
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        lang="ru", started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
        credentials_delivery_status="sent",
        credentials_delivery_template="welcome",
        credentials_delivery_attempts=1,
        credentials_delivered_at=datetime.now(timezone.utc),
    )
    session.add(rental)
    await session.flush()
    return rental


def _ctx(gateway: FakeChatGateway, session: AsyncSession, chat_id: int = 100,
         lang: str = "ru", command: CommandType = CommandType.CODE,
         order_id: str | None = None) -> CommandContext:
    ctx = CommandContext(
        chat_id=chat_id,
        sender_id=200,
        text="!код",
        order_id=order_id,
        lang=lang,
        gateway=gateway,
        parsed=ParsedCommand(command=command, argument=None),
    )
    object.__setattr__(ctx, "_session", session)
    return ctx


async def _add_second_rental_in_same_chat(
    session: AsyncSession,
    first: Rental,
) -> tuple[Rental, Account]:
    account = Account(
        login="acc2",
        password_encrypted="enc-2",
        totp_secret_encrypted="SECONDSECRETTOTP",
        tier_id=first.tier_id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(account)
    order = Order(
        funpay_order_id="o2",
        funpay_chat_id=first.buyer_funpay_chat_id,
        buyer_funpay_id=first.buyer_funpay_id,
        tier_id=first.tier_id,
        duration_id=first.duration_id,
        limit_scope_id=first.limit_scope_id,
        price=100,
        status="pending",
    )
    session.add(order)
    await session.flush()
    rental = Rental(
        order_id=order.id,
        account_id=account.id,
        buyer_funpay_id=first.buyer_funpay_id,
        buyer_funpay_chat_id=first.buyer_funpay_chat_id,
        tier_id=first.tier_id,
        duration_id=first.duration_id,
        limit_scope_id=first.limit_scope_id,
        lang="ru",
        started_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
        credentials_delivery_status="sent",
        credentials_delivery_template="welcome",
        credentials_delivery_attempts=1,
    )
    session.add(rental)
    await session.flush()
    return rental, account


async def test_safe_totp_window_waits_across_near_boundary():
    sleep = AsyncMock()
    with (
        patch("app.services.command_handlers.time.time", return_value=28.0),
        patch("app.services.command_handlers.asyncio.sleep", new=sleep),
    ):
        await _wait_for_safe_totp_window(12)

    sleep.assert_awaited_once_with(pytest.approx(2.05))


async def test_code_handler_sends_totp(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _seed_rental(session)
    gateway = FakeChatGateway()
    handler = CodeHandler(totp_min_validity_s=0)
    ctx = _ctx(gateway, session)
    with patch("app.services.command_handlers.generate_totp", return_value="123456"):
        await handler(ctx)
    assert len(gateway.sent_messages) == 1
    _, text = gateway.sent_messages[0]
    assert "123456" in text


async def test_code_handler_denies_refund_pending_rental(session: AsyncSession):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_rental(session)
    order = await session.get(Order, rental.order_id)
    order.status = "refund_pending"
    await session.flush()
    gateway = FakeChatGateway()

    with patch("app.services.command_handlers.generate_totp") as generate:
        await CodeHandler(totp_min_validity_s=0)(
            _ctx(gateway, session, order_id=order.funpay_order_id)
        )

    generate.assert_not_called()
    assert "Доступ закончился" in gateway.sent_messages[0][1]


async def test_code_handler_uses_exact_order_when_chat_has_two_active_rentals(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    first = await _seed_rental(session)
    _second, second_account = await _add_second_rental_in_same_chat(
        session, first,
    )
    gateway = FakeChatGateway()

    with patch(
        "app.services.command_handlers.generate_totp",
        return_value="654321",
    ) as generate:
        await CodeHandler(totp_min_validity_s=0)(
            _ctx(gateway, session, order_id="o2")
        )

    generate.assert_called_once_with(second_account.totp_secret_encrypted)
    assert "654321" in gateway.sent_messages[0][1]


async def test_code_handler_refuses_to_guess_between_two_active_rentals(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    first = await _seed_rental(session)
    await _add_second_rental_in_same_chat(session, first)
    gateway = FakeChatGateway()

    with patch("app.services.command_handlers.generate_totp") as generate:
        await CodeHandler(totp_min_validity_s=0)(_ctx(gateway, session))

    generate.assert_not_called()
    assert "несколько активных заказов" in gateway.sent_messages[0][1]


async def test_code_handler_rechecks_expiry_after_totp_boundary_wait(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_rental(session)
    gateway = FakeChatGateway()

    async def cross_expiry(_minimum_validity: float) -> None:
        rental.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.flush()

    with (
        patch(
            "app.services.command_handlers._wait_for_safe_totp_window",
            side_effect=cross_expiry,
        ),
        patch("app.services.command_handlers.generate_totp") as generate,
    ):
        await CodeHandler(totp_min_validity_s=12)(_ctx(gateway, session))

    generate.assert_not_called()
    await session.refresh(rental)
    assert rental.status == "expiry_pending"
    assert "Доступ закончился" in gateway.sent_messages[0][1]


async def test_code_handler_sends_labelled_totp_and_fresh_email_otp(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_rental(session)
    account = await session.get(Account, rental.account_id)
    account.email = "owner@example.com"
    account.email_password_encrypted = "mail-password"
    await session.flush()

    received_at = datetime.now(timezone.utc)
    provider = AsyncMock()
    provider.preflight = AsyncMock()
    provider.fetch_fresh_verification_code = AsyncMock(return_value=
        FreshVerificationCode(
            code="654321",
            received_at=received_at,
            fingerprint="f" * 64,
        )
    )
    builder = AsyncMock(return_value=provider)
    gateway = FakeChatGateway()
    handler = CodeHandler(
        email_provider_builder=builder,
        email_timeout_s=0,
        totp_min_validity_s=0,
    )

    with patch("app.services.command_handlers.generate_totp", return_value="123456"):
        await handler(_ctx(gateway, session))

    text = gateway.sent_messages[0][1]
    assert "TOTP (приложение): 123456" in text
    assert "Email OTP OpenAI: 654321" in text
    provider.preflight.assert_not_awaited()
    cutoff = provider.fetch_fresh_verification_code.await_args.kwargs["not_before"]
    assert cutoff >= max(
        rental.started_at.replace(tzinfo=timezone.utc),
        datetime.now(timezone.utc) - timedelta(minutes=10, seconds=2),
    )
    log = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "buyer_email_code_delivered"
            )
        )
    ).scalar_one()
    assert log.metadata_["fingerprint"] == "f" * 64
    assert "654321" not in repr(log.metadata_)
    assert "123456" not in repr(log.metadata_)


async def test_code_handler_rejects_stale_or_duplicate_email_code(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_rental(session)
    account = await session.get(Account, rental.account_id)
    account.email = "owner@example.com"
    account.email_password_encrypted = "mail-password"
    await session.flush()
    provider = AsyncMock()
    provider.preflight = AsyncMock()
    provider.fetch_fresh_verification_code = AsyncMock(return_value=
        FreshVerificationCode(
            code="333333",
            received_at=datetime.now(timezone.utc) - timedelta(minutes=11),
            fingerprint="s" * 64,
        )
    )
    gateway = FakeChatGateway()
    handler = CodeHandler(
        email_provider_builder=AsyncMock(return_value=provider),
        email_timeout_s=0,
        totp_min_validity_s=0,
    )

    with patch("app.services.command_handlers.generate_totp", return_value="111111"):
        await handler(_ctx(gateway, session))
    assert "111111" in gateway.sent_messages[-1][1]
    assert "333333" not in gateway.sent_messages[-1][1]

    provider.fetch_fresh_verification_code.return_value = FreshVerificationCode(
        code="444444",
        received_at=datetime.now(timezone.utc),
        fingerprint="d" * 64,
    )
    rental.last_code_request_at = datetime.now(timezone.utc) - timedelta(seconds=31)
    await session.flush()
    with patch("app.services.command_handlers.generate_totp", return_value="222222"):
        await handler(_ctx(gateway, session))
    assert "Email OTP OpenAI: 444444" in gateway.sent_messages[-1][1]

    rental.last_code_request_at = datetime.now(timezone.utc) - timedelta(seconds=31)
    await session.flush()
    with patch("app.services.command_handlers.generate_totp", return_value="555555"):
        await handler(_ctx(gateway, session))
    assert "555555" in gateway.sent_messages[-1][1]
    assert "444444" not in gateway.sent_messages[-1][1]
    assert "уже выдавался" in gateway.sent_messages[-1][1]
    provider.preflight.assert_not_awaited()


async def test_code_handler_mail_failure_still_returns_totp(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_rental(session)
    account = await session.get(Account, rental.account_id)
    account.email = "owner@example.com"
    account.email_password_encrypted = "mail-password"
    await session.flush()
    provider = AsyncMock()
    provider.fetch_fresh_verification_code = AsyncMock(side_effect=
        EmailProviderError(
            EmailErrorCode.CONNECTION_FAILED,
            "safe mailbox error",
        )
    )
    gateway = FakeChatGateway()
    handler = CodeHandler(
        email_provider_builder=AsyncMock(return_value=provider),
        email_timeout_s=0,
        totp_min_validity_s=0,
    )

    with patch("app.services.command_handlers.generate_totp", return_value="777777"):
        await handler(_ctx(gateway, session))

    text = gateway.sent_messages[0][1]
    assert "TOTP (приложение): 777777" in text
    assert "нужен продавец" in text
    assert "safe mailbox error" not in text


async def test_code_handler_rejects_expired_rental(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    rental = await _seed_rental(session)
    rental.status = "expired"
    await session.flush()
    gateway = FakeChatGateway()
    handler = CodeHandler(totp_min_validity_s=0)
    ctx = _ctx(gateway, session)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_code_handler_no_rental_sends_expired(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = CodeHandler(totp_min_validity_s=0)
    ctx = _ctx(gateway, session, chat_id=999)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_code_handler_antispam_blocks_within_30s(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _seed_rental(session)
    gateway = FakeChatGateway()
    handler = CodeHandler(totp_min_validity_s=0)
    ctx = _ctx(gateway, session)
    with patch("app.services.command_handlers.generate_totp", return_value="111111"):
        await handler(ctx)
    with patch("app.services.command_handlers.generate_totp", return_value="222222"):
        await handler(ctx)
    # Первый — код, второй — rate_limited (без кода)
    assert len(gateway.sent_messages) == 2
    _, second_text = gateway.sent_messages[1]
    assert "222222" not in second_text


async def test_help_handler_sends_help_template(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = HelpHandler()
    ctx = _ctx(gateway, session, command=CommandType.HELP)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_subscription_handler_shows_limits(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _seed_rental(session)
    gateway = FakeChatGateway()
    handler = SubscriptionHandler()
    ctx = _ctx(gateway, session, command=CommandType.SUBSCRIPTION)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_subscription_handler_no_rental_sends_expired(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = SubscriptionHandler()
    ctx = _ctx(gateway, session, chat_id=999, command=CommandType.SUBSCRIPTION)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_seller_handler_responds(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    from unittest.mock import AsyncMock, patch

    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = SellerHandler()
    ctx = _ctx(gateway, session, command=CommandType.SELLER)
    notifier = AsyncMock()
    with patch(
        "app.services.command_handlers.TelegramNotifier.from_settings",
        new=AsyncMock(return_value=notifier),
    ):
        await handler(ctx)
    assert len(gateway.sent_messages) == 1
    notifier.notify_seller_called.assert_awaited_once_with(
        str(ctx.sender_id),
        funpay_chat_id=str(ctx.chat_id),
        order_id=ctx.order_id,
    )


from app.services.command_handlers import ReplaceHandler
from app.services.account_validation import ValidationOutcome
from app.services.kick_service import KickResult


async def _invalid_validator(_session, _account_id):
    return ValidationOutcome.LOGIN_FAILED


async def _healthy_validator(_session, _account_id):
    return ValidationOutcome.OK


class FakeKickService:
    def __init__(self, success: bool = True):
        self.success = success
        self.calls: list[int] = []

    async def kick(self, _session, account_id: int):
        self.calls.append(account_id)
        return KickResult(success=self.success, error=None if self.success else "failed")


async def test_replace_handler_switches_account(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    rental = await _seed_rental(session)
    old_account_id = rental.account_id
    await seed_message_templates(session)

    tier = await session.get(SubscriptionTier, rental.tier_id)
    acc2 = Account(
        login="acc2", password_encrypted="pass2", totp_secret_encrypted="enc_totp",
        tier_id=tier.id, status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc2)
    await session.flush()
    session.add(AccountLimits(
        account_id=acc2.id, refresh_token_encrypted="enc",
        chat_5h_remaining_pct=90, chat_weekly_remaining_pct=80,
        codex_5h_remaining_pct=70, codex_weekly_remaining_pct=60,
        codex_primary_remaining_pct=69,
        codex_primary_window_seconds=5 * 60 * 60,
        codex_primary_resets_at=datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc),
        codex_secondary_remaining_pct=59,
        codex_secondary_window_seconds=7 * 24 * 60 * 60,
        codex_secondary_resets_at=datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
        measured_at=datetime.now(timezone.utc), refresh_status="ok",
        plan_type="plus", plan_window_status="ok",
        expected_long_window_seconds=7 * 24 * 60 * 60,
    ))
    await session.flush()

    gateway = FakeChatGateway()
    kick = FakeKickService()
    handler = ReplaceHandler(validator=_invalid_validator, kick_service=kick)
    ctx = _ctx(gateway, session, command=CommandType.REPLACE)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1
    _, text = gateway.sent_messages[0]
    assert "acc2" in text or "pass2" in text
    await session.refresh(rental)
    assert rental.account_id != old_account_id
    assert rental.replacement_count == 1
    assert rental.credentials_delivery_status == "sent"
    assert rental.credentials_delivery_template == "replace_success"
    assert rental.issued_codex_primary_pct == 69
    assert rental.issued_codex_primary_window_seconds == 5 * 60 * 60
    assert rental.issued_codex_primary_resets_at is not None
    assert rental.issued_codex_primary_resets_at.replace(
        tzinfo=timezone.utc
    ) == datetime(
        2026, 7, 13, 14, 0, tzinfo=timezone.utc
    )
    assert rental.issued_codex_secondary_pct == 59
    assert rental.issued_codex_secondary_window_seconds == 7 * 24 * 60 * 60
    assert rental.issued_codex_secondary_resets_at is not None
    assert rental.issued_codex_secondary_resets_at.replace(
        tzinfo=timezone.utc
    ) == datetime(
        2026, 7, 20, 9, 0, tzinfo=timezone.utc
    )
    assert rental.issued_plan_window_status == "ok"
    assert rental.issued_expected_long_window_seconds == 7 * 24 * 60 * 60
    assert rental.issued_limits_measured_at is not None
    old_account = await session.get(Account, old_account_id)
    assert old_account.status == "maintenance"
    assert kick.calls == [old_account_id]


async def test_replace_handler_no_account_available(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await _seed_rental(session)
    await seed_message_templates(session)

    gateway = FakeChatGateway()
    handler = ReplaceHandler(
        validator=_invalid_validator,
        kick_service=FakeKickService(),
    )
    ctx = _ctx(gateway, session, command=CommandType.REPLACE)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_replace_handler_no_active_rental(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = ReplaceHandler()
    ctx = _ctx(gateway, session, chat_id=999, command=CommandType.REPLACE)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_replace_handler_expires_due_rental_before_validation(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_rental(session)
    rental.expires_at = datetime.now(timezone.utc)
    await session.flush()
    validator = AsyncMock(return_value=ValidationOutcome.LOGIN_FAILED)
    kick = FakeKickService()
    gateway = FakeChatGateway()

    await ReplaceHandler(validator=validator, kick_service=kick)(
        _ctx(gateway, session, command=CommandType.REPLACE)
    )

    validator.assert_not_awaited()
    assert kick.calls == []
    await session.refresh(rental)
    assert rental.status == "expiry_pending"
    assert rental.replacement_count == 0


async def test_replace_handler_rechecks_expiry_before_durable_switch(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_rental(session)
    old_account_id = rental.account_id
    tier = await session.get(SubscriptionTier, rental.tier_id)
    replacement = Account(
        login="late-replacement",
        password_encrypted="pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(replacement)
    await session.flush()

    class ExpiringPool:
        async def acquire_excluding(self, *_args, **_kwargs):
            rental.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.flush()
            return replacement

    gateway = FakeChatGateway()
    handler = ReplaceHandler(
        account_pool=ExpiringPool(),
        validator=_invalid_validator,
        kick_service=FakeKickService(),
    )

    await handler(_ctx(gateway, session, command=CommandType.REPLACE))

    await session.refresh(rental)
    assert rental.status == "expiry_pending"
    assert rental.account_id == old_account_id
    assert rental.replacement_count == 0
    assert "late-replacement" not in gateway.sent_messages[-1][1]


async def test_subscription_handler_expires_due_rental_synchronously(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_rental(session)
    rental.expires_at = datetime.now(timezone.utc)
    await session.flush()
    gateway = FakeChatGateway()

    await SubscriptionHandler()(
        _ctx(gateway, session, command=CommandType.SUBSCRIPTION)
    )

    await session.refresh(rental)
    assert rental.status == "expiry_pending"
    assert "Доступ закончился" in gateway.sent_messages[0][1]


async def test_code_handler_expires_due_rental_before_generating_secret(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates

    await seed_message_templates(session)
    rental = await _seed_rental(session)
    rental.expires_at = datetime.now(timezone.utc)
    await session.flush()
    gateway = FakeChatGateway()

    with patch("app.services.command_handlers.generate_totp") as generate:
        await CodeHandler(totp_min_validity_s=0)(_ctx(gateway, session))

    generate.assert_not_called()
    await session.refresh(rental)
    assert rental.status == "expiry_pending"
    assert len(gateway.sent_messages) == 1
    expiry_audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.event_type == "rental_expired_on_command",
            AuditLog.rental_id == rental.id,
        )
    )
    assert expiry_audit is not None


async def test_replace_handler_declines_healthy_account(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    rental = await _seed_rental(session)
    await seed_message_templates(session)
    kick = FakeKickService()
    gateway = FakeChatGateway()
    handler = ReplaceHandler(validator=_healthy_validator, kick_service=kick)

    await handler(_ctx(gateway, session, command=CommandType.REPLACE))

    await session.refresh(rental)
    assert rental.replacement_count == 0
    assert kick.calls == []


async def test_replace_handler_does_not_issue_second_account(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    rental = await _seed_rental(session)
    rental.replacement_count = 1
    await seed_message_templates(session)
    kick = FakeKickService()
    gateway = FakeChatGateway()
    handler = ReplaceHandler(validator=_invalid_validator, kick_service=kick)

    await handler(_ctx(gateway, session, command=CommandType.REPLACE))

    assert kick.calls == []
    assert len(gateway.sent_messages) == 1


async def test_replace_handler_stops_when_old_credentials_cannot_be_revoked(
    session: AsyncSession,
):
    from app.services.seed_data import seed_message_templates
    rental = await _seed_rental(session)
    old_account_id = rental.account_id
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = ReplaceHandler(
        validator=_invalid_validator,
        kick_service=FakeKickService(success=False),
    )

    await handler(_ctx(gateway, session, command=CommandType.REPLACE))

    await session.refresh(rental)
    assert rental.account_id == old_account_id
    assert rental.replacement_count == 0


async def test_failed_replacement_delivery_retries_same_account_without_extension(
    session: AsyncSession,
):
    from app.services.rental_service import RentalService
    from app.services.seed_data import seed_message_templates

    rental = await _seed_rental(session)
    original_expiry = rental.expires_at
    await seed_message_templates(session)
    tier = await session.get(SubscriptionTier, rental.tier_id)
    replacement = Account(
        login="replacement",
        password_encrypted="replacement-pass",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(replacement)
    await session.flush()
    session.add(AccountLimits(
        account_id=replacement.id,
        refresh_token_encrypted="enc",
        codex_5h_remaining_pct=90,
        codex_weekly_remaining_pct=80,
        measured_at=datetime.now(timezone.utc),
        refresh_status="ok",
        plan_type="plus",
        plan_window_status="ok",
        expected_long_window_seconds=7 * 24 * 60 * 60,
    ))
    await session.flush()

    class FailingGateway(FakeChatGateway):
        async def send_message(self, chat_id: int, text: str) -> int:
            raise RuntimeError("temporary FunPay failure")

    handler = ReplaceHandler(
        validator=_invalid_validator,
        kick_service=FakeKickService(),
    )
    with pytest.raises(RuntimeError, match="temporary FunPay failure"):
        await handler(_ctx(FailingGateway(), session, command=CommandType.REPLACE))

    await session.refresh(rental)
    claimed_account_id = rental.account_id
    assert claimed_account_id == replacement.id
    assert rental.replacement_count == 1
    assert rental.credentials_delivery_status == "failed"
    assert rental.credentials_delivery_template == "replace_success"

    # Durable delivery retries use exponential backoff.  Move the scheduled
    # retry into the past to exercise the retry claim without sleeping.
    rental.credentials_delivery_next_attempt_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    await session.commit()

    retry_gateway = FakeChatGateway()
    await RentalService().fulfill_order(
        session,
        retry_gateway,
        rental.order_id,
        default_max_active_rentals=1,
    )

    await session.refresh(rental)
    assert rental.account_id == claimed_account_id
    assert rental.replacement_count == 1
    assert rental.credentials_delivery_status == "sent"
    assert rental.credentials_delivery_attempts == 3
    assert rental.expires_at.replace(tzinfo=timezone.utc) == original_expiry
    assert "replacement" in retry_gateway.sent_messages[0][1]
