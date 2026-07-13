from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.integrations.funpay.types import (
    BuyerProfileInfo,
    MessageInfo,
    OrderInfo,
    SalePreviewInfo,
    SaleStatus,
)
from app.models.chat import ChatConversation, ChatMessage
from app.models.funpay_sale import FunPaySale, FunPaySaleSyncState
from app.models.rental import Order, Rental
from app.services.chat_service import ChatService, UnverifiedConversationError
from app.services.sale_registry import SaleRegistryService


def _preview(order_id: str, buyer_id: int, *, minutes_ago: int = 0) -> SalePreviewInfo:
    return SalePreviewInfo(
        order_id=order_id,
        status=SaleStatus.PAID,
        buyer_id=buyer_id,
        buyer_username=f"buyer-{buyer_id}",
        buyer_avatar_url=f"https://example.test/{buyer_id}.png",
        buyer_is_online=False,
        buyer_status_text="был недавно",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


async def test_preview_import_survives_order_page_rate_limits(session: AsyncSession):
    class RateLimitedGateway(FakeChatGateway):
        async def get_order(self, order_id: str) -> OrderInfo:
            raise RuntimeError("rate limited")

    gateway = RateLimitedGateway()
    gateway.set_sales([
        _preview("sale-1", 101),
        _preview("sale-2", 102, minutes_ago=1),
        _preview("sale-3", 103, minutes_ago=2),
    ])

    result = await SaleRegistryService().sync_recent_sales(
        session,
        gateway,
        detail_limit=2,
    )

    sales = list((await session.execute(select(FunPaySale))).scalars())
    assert {item.funpay_order_id for item in sales} == {
        "sale-1",
        "sale-2",
        "sale-3",
    }
    assert all(item.funpay_chat_id is None for item in sales)
    assert result.imported == 3
    assert result.enriched == 0
    assert result.enrichment_errors == 2


async def test_exact_order_sync_filters_and_enriches_one_sale(session: AsyncSession):
    gateway = FakeChatGateway()
    gateway.set_sales([_preview("wrong", 1), _preview("target", 2)])
    gateway.set_order(OrderInfo(
        order_id="target",
        status=SaleStatus.PAID,
        chat_id=222,
        buyer_id=2,
        subcategory_id=55,
        title="T",
        price=10,
        buyer_username="buyer-two",
        buyer_avatar_url="https://example.test/two.png",
        buyer_is_online=True,
        buyer_status_text="online",
    ))

    result = await SaleRegistryService().sync_order(session, gateway, "target")

    sales = list((await session.execute(select(FunPaySale))).scalars())
    assert [item.funpay_order_id for item in sales] == ["target"]
    assert sales[0].funpay_chat_id == "222"
    assert result.enriched == 1
    conversation = (
        await session.execute(select(ChatConversation))
    ).scalar_one()
    assert conversation.verified_sale is True
    assert conversation.buyer_username == "buyer-two"


async def test_plain_buyer_message_rebinds_chat_and_preserves_history(
    session: AsyncSession,
):
    sale = FunPaySale(
        funpay_order_id="sale-old-chat",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_username="buyer",
        status="paid",
    )
    session.add(sale)
    await session.flush()
    service = ChatService()
    first, _ = await service.record_event(session, MessageInfo(
        message_id=1,
        chat_id=100,
        sender_id=200,
        sender_username="buyer",
        text="first",
        order_id="sale-old-chat",
    ))

    second, _ = await service.record_event(session, MessageInfo(
        message_id=2,
        chat_id=999,
        sender_id=200,
        sender_username="buyer",
        text="code again",
        order_id=None,
    ))
    await session.flush()

    conversations = list(
        (await session.execute(select(ChatConversation))).scalars()
    )
    messages = list((await session.execute(select(ChatMessage))).scalars())
    await session.refresh(sale)
    assert len(conversations) == 1
    assert conversations[0].funpay_chat_id == "999"
    assert first.conversation_id == second.conversation_id == conversations[0].id
    assert len(messages) == 2
    assert sale.funpay_chat_id == "999"


async def test_inbox_groups_sales_and_sorts_newest_sale_without_messages(
    session: AsyncSession,
):
    now = datetime.now(timezone.utc)
    session.add_all([
        FunPaySale(
            funpay_order_id="HHHGNZ4N",
            funpay_chat_id="10",
            buyer_funpay_id="1",
            buyer_username="latest-buyer",
            status="paid",
            created_at=now,
        ),
        FunPaySale(
            funpay_order_id="OLDER-SAME-BUYER",
            funpay_chat_id=None,
            buyer_funpay_id="1",
            buyer_username="latest-buyer",
            status="completed",
            created_at=now - timedelta(days=1),
        ),
        FunPaySale(
            funpay_order_id="OTHER",
            funpay_chat_id="20",
            buyer_funpay_id="2",
            buyer_username="other",
            status="paid",
            created_at=now - timedelta(hours=1),
        ),
    ])
    session.add_all([
        ChatConversation(
            funpay_chat_id="10", buyer_funpay_id="1", verified_sale=True
        ),
        ChatConversation(
            funpay_chat_id="20", buyer_funpay_id="2", verified_sale=True
        ),
    ])
    await session.flush()

    inbox = await ChatService().list_conversations(session)

    assert [item.buyer_funpay_id for item in inbox] == ["1", "2"]
    assert [item.funpay_order_id for item in inbox[0].sale_orders] == [
        "HHHGNZ4N",
        "OLDER-SAME-BUYER",
    ]


async def _seed_bound_sale_state(
    session: AsyncSession,
    *,
    order_id: str,
) -> tuple[FunPaySale, Order, Rental]:
    sale = FunPaySale(
        funpay_order_id=order_id,
        funpay_chat_id="100",
        buyer_funpay_id="200",
        status="paid",
    )
    order = Order(
        funpay_order_id=order_id,
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        price=100,
        status="pending",
    )
    session.add_all([sale, order])
    await session.flush()
    sale.order_id = order.id
    rental = Rental(
        order_id=order.id,
        account_id=1,
        buyer_funpay_id="200",
        buyer_funpay_chat_id="100",
        tier_id=1,
        duration_id=1,
        limit_scope_id=1,
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        status="active",
    )
    session.add(rental)
    await session.flush()
    return sale, order, rental


async def test_zero_chat_id_is_rejected_without_rebinding_any_sale_state(
    session: AsyncSession,
):
    sale, order, rental = await _seed_bound_sale_state(
        session,
        order_id="sale-zero-guard",
    )

    with pytest.raises(UnverifiedConversationError):
        await ChatService().record_event(session, MessageInfo(
            message_id=900,
            chat_id=0,
            sender_id=200,
            sender_username="buyer",
            text="must not bind to node zero",
            order_id=None,
        ))

    assert sale.funpay_chat_id == "100"
    assert order.funpay_chat_id == "100"
    assert rental.buyer_funpay_chat_id == "100"
    assert list((await session.execute(select(ChatConversation))).scalars()) == []
    assert list((await session.execute(select(ChatMessage))).scalars()) == []


@pytest.mark.parametrize(
    ("from_me", "sender_id"),
    [(True, 999), (False, None)],
)
async def test_exact_order_cannot_rebind_without_verified_inbound_sender(
    session: AsyncSession,
    from_me: bool,
    sender_id: int | None,
):
    sale, order, rental = await _seed_bound_sale_state(
        session,
        order_id="sale-exact-rebind-guard",
    )

    with pytest.raises(UnverifiedConversationError):
        await ChatService().record_event(session, MessageInfo(
            message_id=901 if from_me else 902,
            chat_id=999,
            sender_id=sender_id,
            text="must not move the verified buyer chat",
            order_id="sale-exact-rebind-guard",
            from_me=from_me,
        ))

    assert sale.funpay_chat_id == "100"
    assert order.funpay_chat_id == "100"
    assert rental.buyer_funpay_chat_id == "100"
    assert list((await session.execute(select(ChatConversation))).scalars()) == []


async def test_exact_order_rebinds_for_verified_inbound_buyer(
    session: AsyncSession,
):
    sale, order, rental = await _seed_bound_sale_state(
        session,
        order_id="sale-exact-valid-rebind",
    )

    stored, created = await ChatService().record_event(session, MessageInfo(
        message_id=903,
        chat_id=999,
        sender_id=200,
        sender_username="buyer",
        text="valid buyer message",
        order_id="sale-exact-valid-rebind",
    ))

    assert created is True
    assert stored.direction == "incoming"
    assert sale.funpay_chat_id == "999"
    assert order.funpay_chat_id == "999"
    assert rental.buyer_funpay_chat_id == "999"


async def test_historical_sale_backfill_uses_persisted_bounded_cursor(
    session: AsyncSession,
):
    previews = [
        _preview(f"sale-{index:03d}", 10_000 + index, minutes_ago=index)
        for index in range(205)
    ]
    gateway = FakeChatGateway()
    gateway.set_sales(previews, page_size=100)
    service = SaleRegistryService()

    # Batch import must not regress to one SELECT/flush per preview.
    service._get_by_remote_order = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("per-preview lookup is forbidden")
    )
    first = await service.sync_recent_sales(
        session,
        gateway,
        detail_limit=0,
    )
    state = await session.get(FunPaySaleSyncState, 1)
    assert first.imported == 200
    assert state is not None
    assert state.backfill_cursor == "sale-200"
    assert state.backfill_complete is False
    assert len(gateway.sales_list_calls) == 2

    # A fresh service instance resumes the durable cursor, while also reading
    # one head page for newly arrived sales.
    second = await SaleRegistryService().sync_recent_sales(
        session,
        gateway,
        detail_limit=0,
    )
    await session.refresh(state)
    assert second.imported == 5
    assert state.backfill_cursor is None
    assert state.backfill_complete is True
    assert len(gateway.sales_list_calls) == 4
    assert len(list((await session.execute(select(FunPaySale))).scalars())) == 205

    # Once completed, recurring 120-second syncs fetch only the current head;
    # they never traverse all historical pages again.
    third = await SaleRegistryService().sync_recent_sales(
        session,
        gateway,
        detail_limit=0,
    )
    assert third.imported == 0
    assert len(gateway.sales_list_calls) == 5


async def test_detail_enrichment_backoff_prevents_permanent_error_starvation(
    session: AsyncSession,
):
    class PartiallyBrokenGateway(FakeChatGateway):
        def __init__(self) -> None:
            super().__init__()
            self.detail_calls: list[str] = []

        async def get_order(self, order_id: str) -> OrderInfo:
            self.detail_calls.append(order_id)
            if order_id != "detail-1":
                raise RuntimeError("permanent parser failure")
            return OrderInfo(
                order_id=order_id,
                status=SaleStatus.PAID,
                chat_id=601,
                buyer_id=5001,
                subcategory_id=55,
                title="T",
                price=100,
                buyer_username="working-buyer",
            )

    gateway = PartiallyBrokenGateway()
    gateway.set_sales([
        _preview(f"detail-{index}", 5000 + index, minutes_ago=index)
        for index in range(5)
    ])

    first = await SaleRegistryService().sync_recent_sales(session, gateway)
    await session.commit()
    assert first.enrichment_errors == 4
    assert gateway.detail_calls == [
        "detail-0",  # reserved newest slot
        "detail-4", "detail-3", "detail-2",  # durable oldest slots
    ]
    deferred = list(
        (
            await session.execute(
                select(FunPaySale)
                .where(FunPaySale.funpay_order_id.in_(
                    ["detail-0", "detail-2", "detail-3", "detail-4"]
                ))
                .order_by(FunPaySale.funpay_order_id)
            )
        ).scalars()
    )
    assert all(item.detail_attempts == 1 for item in deferred)
    assert all(item.detail_next_attempt_at is not None for item in deferred)

    # Simulate a process restart: retry state is loaded from the database, not
    # an in-memory queue. Deferred failures cannot occupy the next batch.
    session.expunge_all()
    second = await SaleRegistryService().sync_recent_sales(session, gateway)
    await session.commit()

    assert second.enriched == 1
    assert gateway.detail_calls[-1] == "detail-1"
    assert len(gateway.detail_calls) == 5
    working = await session.scalar(
        select(FunPaySale).where(FunPaySale.funpay_order_id == "detail-1")
    )
    assert working is not None
    assert working.funpay_chat_id == "601"
    assert working.detail_attempts == 0
    assert working.detail_next_attempt_at is None


async def test_continuous_new_sales_do_not_starve_historical_detail_queue(
    session: AsyncSession,
):
    class RecordingGateway(FakeChatGateway):
        def __init__(self) -> None:
            super().__init__()
            self.detail_calls: list[str] = []

        async def get_order(self, order_id: str) -> OrderInfo:
            self.detail_calls.append(order_id)
            return await super().get_order(order_id)

    gateway = RecordingGateway()
    old = [
        _preview(f"old-{index}", 6000 + index, minutes_ago=index + 10)
        for index in range(8)
    ]
    all_previews = list(old)
    for preview in old:
        gateway.set_order(OrderInfo(
            order_id=preview.order_id,
            status=SaleStatus.PAID,
            chat_id=70_000 + preview.buyer_id,
            buyer_id=preview.buyer_id,
            subcategory_id=55,
            title="T",
            price=100,
        ))

    for cycle in range(3):
        newest = _preview(f"new-{cycle}", 7000 + cycle)
        all_previews.insert(0, newest)
        gateway.set_order(OrderInfo(
            order_id=newest.order_id,
            status=SaleStatus.PAID,
            chat_id=80_000 + newest.buyer_id,
            buyer_id=newest.buyer_id,
            subcategory_id=55,
            title="T",
            price=100,
        ))
        gateway.set_sales(all_previews)
        await SaleRegistryService().sync_recent_sales(session, gateway)
        await session.commit()

    historical = list((await session.execute(
        select(FunPaySale).where(
            FunPaySale.funpay_order_id.in_([item.order_id for item in old])
        )
    )).scalars())
    assert all(item.funpay_chat_id is not None for item in historical)
    assert len(gateway.detail_calls) <= 3 * SaleRegistryService.DEFAULT_DETAIL_BATCH


async def test_profile_round_robin_reaches_beyond_head_and_survives_restart(
    session: AsyncSession,
):
    now = datetime.now(timezone.utc)
    rows: list[FunPaySale | ChatConversation] = []
    for buyer_id in range(1, 102):
        checked_at = now
        if buyer_id == 100:
            checked_at = now - timedelta(minutes=20)
        elif buyer_id == 101:
            checked_at = now - timedelta(minutes=30)
        rows.extend([
            FunPaySale(
                funpay_order_id=f"profile-{buyer_id}",
                funpay_chat_id=str(10_000 + buyer_id),
                buyer_funpay_id=str(buyer_id),
                buyer_username=f"old-{buyer_id}",
                buyer_avatar_url="https://old.test/avatar.png",
                buyer_is_online=True,
                buyer_status_text="online",
                profile_checked_at=checked_at,
                status="paid",
            ),
            ChatConversation(
                funpay_chat_id=str(10_000 + buyer_id),
                buyer_funpay_id=str(buyer_id),
                buyer_username=f"old-{buyer_id}",
                buyer_avatar_url="https://old.test/avatar.png",
                buyer_is_online=True,
                buyer_status_text="online",
                profile_checked_at=checked_at,
                verified_sale=True,
            ),
        ])
    # A duplicate verified conversation for the same historical buyer must be
    # refreshed by the same single profile request.
    rows.extend([
        FunPaySale(
            funpay_order_id="profile-101-second",
            funpay_chat_id="20101",
            buyer_funpay_id="101",
            status="completed",
            profile_checked_at=now - timedelta(minutes=30),
        ),
        ChatConversation(
            funpay_chat_id="20101",
            buyer_funpay_id="101",
            buyer_is_online=True,
            buyer_status_text="online",
            profile_checked_at=now - timedelta(minutes=30),
            verified_sale=True,
        ),
    ])
    session.add_all(rows)
    await session.flush()

    gateway = FakeChatGateway()
    gateway.set_buyer_profile(BuyerProfileInfo(
        buyer_id=101,
        username="  buyer-101  ",
        avatar_url="   ",
        is_online=False,
        status_text=None,
    ))
    gateway.set_buyer_profile(BuyerProfileInfo(
        buyer_id=100,
        username="buyer-100",
        avatar_url="https://new.test/100.png",
        is_online=True,
        status_text="онлайн",
    ))

    first = await SaleRegistryService().refresh_buyer_profiles(session, gateway)
    await session.commit()
    assert first.refreshed == 1
    assert gateway.profile_calls == [101]
    buyer_101_chats = list((await session.execute(
        select(ChatConversation).where(ChatConversation.buyer_funpay_id == "101")
    )).scalars())
    assert len(buyer_101_chats) == 2
    assert all(item.buyer_username == "buyer-101" for item in buyer_101_chats)
    assert all(item.buyer_avatar_url is None for item in buyer_101_chats)
    assert all(item.buyer_is_online is False for item in buyer_101_chats)
    assert all(item.buyer_status_text is None for item in buyer_101_chats)

    session.expunge_all()
    second = await SaleRegistryService().refresh_buyer_profiles(session, gateway)
    await session.commit()
    assert second.refreshed == 1
    assert gateway.profile_calls == [101, 100]


async def test_bad_profile_identity_is_deferred_without_starving_next_buyer(
    session: AsyncSession,
):
    for buyer_id in (201, 202):
        session.add_all([
            FunPaySale(
                funpay_order_id=f"poison-{buyer_id}",
                funpay_chat_id=str(30_000 + buyer_id),
                buyer_funpay_id=str(buyer_id),
                status="paid",
            ),
            ChatConversation(
                funpay_chat_id=str(30_000 + buyer_id),
                buyer_funpay_id=str(buyer_id),
                buyer_username=f"original-{buyer_id}",
                buyer_avatar_url=f"https://old.test/{buyer_id}.png",
                buyer_is_online=True,
                buyer_status_text="online",
                verified_sale=True,
            ),
        ])
    await session.flush()

    class IdentityGateway(FakeChatGateway):
        async def get_buyer_profile(self, buyer_id: int) -> BuyerProfileInfo:
            self.profile_calls.append(buyer_id)
            if buyer_id == 201:
                return BuyerProfileInfo(
                    buyer_id=999,
                    username="wrong-user",
                    avatar_url=None,
                    is_online=True,
                    status_text="online",
                )
            return BuyerProfileInfo(
                buyer_id=buyer_id,
                username=f"buyer-{buyer_id}",
                avatar_url=None,
                is_online=False,
                status_text=None,
            )

    gateway = IdentityGateway()
    first = await SaleRegistryService().refresh_buyer_profiles(session, gateway)
    await session.commit()
    poisoned = await session.scalar(select(ChatConversation).where(
        ChatConversation.buyer_funpay_id == "201"
    ))
    assert first.errors == 1
    assert poisoned is not None
    assert poisoned.buyer_username == "original-201"
    assert poisoned.buyer_avatar_url == "https://old.test/201.png"
    assert poisoned.buyer_is_online is None
    assert poisoned.buyer_status_text is None
    assert poisoned.profile_attempts == 1
    assert poisoned.profile_next_attempt_at is not None
    poisoned_sale = await session.scalar(select(FunPaySale).where(
        FunPaySale.funpay_order_id == "poison-201"
    ))
    assert poisoned_sale is not None
    assert poisoned_sale.buyer_is_online is None
    assert poisoned_sale.buyer_status_text is None

    # A later verified inbound message re-runs conversation verification. It
    # must not resurrect stale presence from the sale cache after a failed
    # profile request.
    await ChatService().record_event(session, MessageInfo(
        message_id=20_201,
        chat_id=30_201,
        sender_id=201,
        text="code again",
        order_id="poison-201",
    ))
    await session.refresh(poisoned)
    assert poisoned.buyer_is_online is None
    assert poisoned.buyer_status_text is None

    session.expunge_all()
    second = await SaleRegistryService().refresh_buyer_profiles(session, gateway)
    assert second.refreshed == 1
    assert gateway.profile_calls == [201, 202]


async def test_newer_preview_clears_nullable_profile_and_retry_state(
    session: AsyncSession,
):
    old_checked = datetime.now(timezone.utc) - timedelta(hours=1)
    sale = FunPaySale(
        funpay_order_id="nullable-profile",
        funpay_chat_id="401",
        buyer_funpay_id="501",
        buyer_username="old-name",
        buyer_avatar_url="https://old.test/avatar.png",
        buyer_is_online=True,
        buyer_status_text="online",
        profile_checked_at=old_checked,
        status="paid",
    )
    conversation = ChatConversation(
        funpay_chat_id="401",
        buyer_funpay_id="501",
        buyer_username="old-name",
        buyer_avatar_url="https://old.test/avatar.png",
        buyer_is_online=True,
        buyer_status_text="online",
        profile_checked_at=old_checked,
        profile_attempts=3,
        profile_next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1),
        verified_sale=True,
    )
    session.add_all([sale, conversation])
    await session.flush()
    gateway = FakeChatGateway()
    gateway.set_sales([SalePreviewInfo(
        order_id="nullable-profile",
        status=SaleStatus.PAID,
        buyer_id=501,
        buyer_username="new-name",
        buyer_avatar_url=None,
        buyer_is_online=False,
        buyer_status_text=None,
        created_at=datetime.now(timezone.utc),
    )])

    await SaleRegistryService().sync_recent_sales(
        session,
        gateway,
        detail_limit=0,
    )

    assert conversation.buyer_username == "new-name"
    assert conversation.buyer_avatar_url is None
    assert conversation.buyer_is_online is False
    assert conversation.buyer_status_text is None
    assert conversation.profile_attempts == 0
    assert conversation.profile_next_attempt_at is None


async def test_global_page_backoff_stops_detail_and_profile_hammering(
    session: AsyncSession,
):
    class RateLimitedError(RuntimeError):
        status = 429

    class SharedBudgetGateway(FakeChatGateway):
        def __init__(self) -> None:
            super().__init__()
            self.detail_calls: list[str] = []
            self.fail = True

        async def get_order(self, order_id: str) -> OrderInfo:
            self.detail_calls.append(order_id)
            if self.fail:
                raise RateLimitedError("slow down")
            return await super().get_order(order_id)

    state = FunPaySaleSyncState(id=1, page_backoff_attempts=99)
    session.add_all([
        state,
        FunPaySale(
            funpay_order_id="profile-after-backoff",
            funpay_chat_id="909",
            buyer_funpay_id="919",
            status="paid",
        ),
        ChatConversation(
            funpay_chat_id="909",
            buyer_funpay_id="919",
            verified_sale=True,
        ),
    ])
    await session.flush()
    gateway = SharedBudgetGateway()
    previews = [_preview(f"limited-{index}", 8000 + index) for index in range(5)]
    gateway.set_sales(previews)
    for index, preview in enumerate(previews):
        gateway.set_order(OrderInfo(
            order_id=preview.order_id,
            status=SaleStatus.PAID,
            chat_id=90_000 + index,
            buyer_id=preview.buyer_id,
            subcategory_id=55,
            title="T",
            price=100,
        ))
    gateway.set_buyer_profile(BuyerProfileInfo(
        buyer_id=919,
        username="buyer-919",
        avatar_url=None,
        is_online=False,
        status_text=None,
    ))

    first = await SaleRegistryService().sync_recent_sales(session, gateway)
    skipped = await SaleRegistryService().refresh_buyer_profiles(session, gateway)
    assert first.enrichment_errors == 1
    assert len(gateway.detail_calls) == 1
    assert skipped.refreshed == 0
    assert gateway.profile_calls == []
    assert state.page_backoff_attempts == 100
    assert state.page_backoff_until is not None
    backoff = state.page_backoff_until
    if backoff.tzinfo is None:
        backoff = backoff.replace(tzinfo=timezone.utc)
    assert backoff - datetime.now(timezone.utc) <= timedelta(hours=24)

    # Simulate a restart after the durable pause expires. Four detail pages
    # plus one profile page keep the shared expensive-page budget at five.
    state.page_backoff_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()
    gateway.fail = False
    session.expunge_all()
    detail_calls_before = len(gateway.detail_calls)
    second = await SaleRegistryService().sync_recent_sales(session, gateway)
    profile = await SaleRegistryService().refresh_buyer_profiles(session, gateway)
    state = await session.get(FunPaySaleSyncState, 1)
    assert second.enriched <= SaleRegistryService.DEFAULT_DETAIL_BATCH
    assert len(gateway.detail_calls) - detail_calls_before <= 4
    assert profile.refreshed == 1
    assert gateway.profile_calls == [919]
    assert state is not None
    assert state.page_backoff_attempts == 0
    assert state.page_backoff_until is None
    assert len(gateway.detail_calls) - detail_calls_before + len(
        gateway.profile_calls
    ) <= 5
