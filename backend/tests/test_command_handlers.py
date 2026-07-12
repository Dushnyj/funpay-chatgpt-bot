from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Order, Rental
from app.services.command_handlers import CodeHandler, HelpHandler, SubscriptionHandler, SellerHandler
from app.services.command_router import CommandContext
from app.services.command_parser import CommandType, ParsedCommand


async def _seed_rental(session: AsyncSession, chat_id: int = 100) -> Rental:
    tier = SubscriptionTier(name="Plus", is_active=True)
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
    )
    session.add(rental)
    await session.flush()
    return rental


def _ctx(gateway: FakeChatGateway, session: AsyncSession, chat_id: int = 100,
         lang: str = "ru", command: CommandType = CommandType.CODE) -> CommandContext:
    ctx = CommandContext(
        chat_id=chat_id,
        sender_id=200,
        text="!код",
        order_id=None,
        lang=lang,
        gateway=gateway,
        parsed=ParsedCommand(command=command, argument=None),
    )
    object.__setattr__(ctx, "_session", session)
    return ctx


async def test_code_handler_sends_totp(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _seed_rental(session)
    gateway = FakeChatGateway()
    handler = CodeHandler()
    ctx = _ctx(gateway, session)
    with patch("app.services.command_handlers.generate_totp", return_value="123456"):
        await handler(ctx)
    assert len(gateway.sent_messages) == 1
    _, text = gateway.sent_messages[0]
    assert "123456" in text


async def test_code_handler_rejects_expired_rental(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    rental = await _seed_rental(session)
    rental.status = "expired"
    await session.flush()
    gateway = FakeChatGateway()
    handler = CodeHandler()
    ctx = _ctx(gateway, session)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_code_handler_no_rental_sends_expired(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = CodeHandler()
    ctx = _ctx(gateway, session, chat_id=999)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1


async def test_code_handler_antispam_blocks_within_30s(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _seed_rental(session)
    gateway = FakeChatGateway()
    handler = CodeHandler()
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
    await seed_message_templates(session)
    gateway = FakeChatGateway()
    handler = SellerHandler()
    ctx = _ctx(gateway, session, command=CommandType.SELLER)
    await handler(ctx)
    assert len(gateway.sent_messages) == 1
