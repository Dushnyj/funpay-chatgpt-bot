from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
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


logger = logging.getLogger(__name__)

_DETAIL_RETRY_BASE = timedelta(minutes=5)
_DETAIL_RETRY_MAX = timedelta(hours=24)
_PROFILE_RETRY_BASE = timedelta(minutes=5)
_PROFILE_RETRY_MAX = timedelta(hours=24)
_PROFILE_REFRESH_TTL = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _status_value(value: SaleStatus | str) -> str:
    return value.value if isinstance(value, SaleStatus) else str(value)


def _status_from_local_order(value: str) -> str:
    return {
        "pending": SaleStatus.PAID.value,
        "completed": SaleStatus.COMPLETED.value,
        "refunded": SaleStatus.REFUNDED.value,
        "refund_pending": SaleStatus.REFUNDED.value,
    }.get(value, SaleStatus.UNKNOWN.value)


@dataclass(frozen=True, slots=True)
class SalesSyncResult:
    imported: int
    enriched: int
    enrichment_errors: int
    history_errors: int = 0
    profiles_refreshed: int = 0
    profile_errors: int = 0


@dataclass(frozen=True, slots=True)
class ProfileRefreshResult:
    refreshed: int
    errors: int


class InvalidSaleProvenanceError(ValueError):
    """The purported sale lacks a stable buyer/order identity."""


class SaleRegistryService:
    """Persist sale-only buyer provenance and keep buyer profiles fresh."""

    DEFAULT_DETAIL_BATCH = 4
    DEFAULT_HISTORY_PAGES = 2
    # ProfilePage parses offers, reviews and chat in addition to presence. One
    # request per 120-second cycle is the safe complement to four detail pages.
    DEFAULT_PROFILE_BATCH = 1

    async def register_new_sale(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: str,
        *,
        info: OrderInfo | None = None,
    ) -> tuple[FunPaySale, OrderInfo]:
        """Record NewSale provenance before fulfillment touches local orders."""

        info = info or await gateway.get_order(order_id)
        if info.order_id != order_id or info.buyer_id <= 0 or info.chat_id <= 0:
            raise InvalidSaleProvenanceError(
                f"Incomplete sale identity for order {order_id}"
            )
        sale = await self._get_by_remote_order(session, order_id)
        if sale is None:
            candidate = FunPaySale(
                funpay_order_id=order_id,
                funpay_chat_id=str(info.chat_id),
                buyer_funpay_id=str(info.buyer_id),
                status=_status_value(info.status),
            )
            try:
                async with session.begin_nested():
                    session.add(candidate)
                    await session.flush()
                sale = candidate
            except IntegrityError:
                # A periodic preview sync may have inserted the same sale
                # after our initial SELECT. Roll back only the savepoint,
                # refetch the winner, and continue fulfillment.
                sale = await self._get_by_remote_order(session, order_id)
                if sale is None:
                    raise
        if sale.buyer_funpay_id != str(info.buyer_id):
            raise InvalidSaleProvenanceError(
                f"Buyer identity changed for sale {order_id}"
            )

        self._apply_order_info(sale, info)
        local_order = await session.scalar(
            select(Order).where(Order.funpay_order_id == order_id)
        )
        if local_order is not None:
            sale.order_id = local_order.id
        await session.flush()
        await self._verify_conversation(session, sale)
        return sale, info

    async def attach_local_order(
        self,
        session: AsyncSession,
        sale: FunPaySale,
        order: Order,
    ) -> None:
        if sale.funpay_order_id != order.funpay_order_id:
            raise InvalidSaleProvenanceError("Local and remote order IDs differ")
        sale.order_id = order.id
        if sale.funpay_chat_id is None:
            sale.funpay_chat_id = order.funpay_chat_id
        await session.flush()
        await self._verify_conversation(session, sale)

    async def bootstrap_from_orders(self, session: AsyncSession) -> int:
        """Import legacy Order rows created by the old NewSale-only callback."""

        orders = list((await session.execute(select(Order))).scalars())
        imported = 0
        for order in orders:
            sale = await self._get_by_remote_order(session, order.funpay_order_id)
            if sale is None:
                sale = FunPaySale(
                    funpay_order_id=order.funpay_order_id,
                    order_id=order.id,
                    funpay_chat_id=order.funpay_chat_id,
                    buyer_funpay_id=order.buyer_funpay_id,
                    status=_status_from_local_order(order.status),
                    created_at=order.created_at,
                )
                session.add(sale)
                imported += 1
                await session.flush()
            else:
                sale.order_id = order.id
                if sale.funpay_chat_id is None:
                    sale.funpay_chat_id = order.funpay_chat_id
            await self._verify_conversation(session, sale)
        return imported

    async def sync_recent_sales(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        *,
        limit: int = 100,
        detail_limit: int = DEFAULT_DETAIL_BATCH,
        order_id: str | None = None,
        history_pages: int = DEFAULT_HISTORY_PAGES,
    ) -> SalesSyncResult:
        """Import previews first, then enrich only a small missing-chat batch.

        Preview rows are never lost merely because FunPay rate-limits one order
        page.  A later cycle resumes enrichment from rows whose chat is null.
        """

        history_errors = 0
        pages = []
        sync_state: FunPaySaleSyncState | None = None
        if order_id is not None:
            pages.append(
                await gateway.list_sales(
                    limit=limit,
                    order_id=order_id,
                    cursor=None,
                )
            )
        else:
            sync_state = await self._get_sync_state(session)
            head = await gateway.list_sales(limit=limit, cursor=None)
            pages.append(head)
            sync_state.head_synced_at = _utcnow()

            if not sync_state.backfill_complete:
                cursor = sync_state.backfill_cursor or head.next_cursor
                seen_cursors: set[str] = set()
                remaining = max(0, history_pages - 1)
                while cursor is not None and remaining > 0:
                    if cursor in seen_cursors:
                        logger.warning(
                            "FunPay sale backfill cursor stopped progressing: %s",
                            cursor,
                        )
                        break
                    seen_cursors.add(cursor)
                    try:
                        page = await gateway.list_sales(
                            limit=limit,
                            cursor=cursor,
                        )
                    except Exception as exc:
                        history_errors += 1
                        logger.warning(
                            "FunPay sale history page deferred cursor=%s error=%s",
                            cursor,
                            type(exc).__name__,
                        )
                        break
                    pages.append(page)
                    next_cursor = page.next_cursor
                    if next_cursor == cursor:
                        logger.warning(
                            "FunPay sale backfill returned the same cursor: %s",
                            cursor,
                        )
                        break
                    cursor = next_cursor
                    remaining -= 1
                sync_state.backfill_cursor = cursor
                sync_state.backfill_complete = cursor is None
            sync_state.updated_at = _utcnow()

        previews_by_order: dict[str, SalePreviewInfo] = {}
        for page in pages:
            for preview in page.sales:
                previews_by_order.setdefault(preview.order_id, preview)
        preview_sales, imported = await self._upsert_previews(
            session,
            list(previews_by_order.values()),
        )
        await self._reconcile_preview_conversations(session, preview_sales)

        # Exact-order QA sync force-enriches only that order. The normal queue
        # reserves one slot for the newest due sale and fills the rest oldest
        # first. This discovers current sales promptly without allowing a
        # continuous arrival stream to starve historical never-tried rows.
        detail_now = _utcnow()
        if sync_state is None:
            sync_state = await self._get_sync_state(session)
        if self._future(sync_state.page_backoff_until, detail_now):
            detail_candidates = []
        elif order_id is not None:
            detail_candidates = [
                sale for sale in preview_sales if sale.funpay_chat_id is None
            ]
        else:
            queue_limit = max(0, detail_limit)
            oldest = list(
                (
                    await session.execute(
                        select(FunPaySale)
                        .where(
                            FunPaySale.funpay_chat_id.is_(None),
                            or_(
                                FunPaySale.detail_next_attempt_at.is_(None),
                                FunPaySale.detail_next_attempt_at <= detail_now,
                            ),
                        )
                        .order_by(
                            FunPaySale.detail_attempts.asc(),
                            FunPaySale.detail_next_attempt_at.asc().nulls_first(),
                            FunPaySale.created_at.asc(),
                            FunPaySale.id.asc(),
                        )
                        .limit(queue_limit)
                    )
                ).scalars()
            )
            detail_candidates = oldest[:queue_limit]
            if queue_limit > 1:
                newest = await session.scalar(
                    select(FunPaySale)
                    .where(
                        FunPaySale.funpay_chat_id.is_(None),
                        or_(
                            FunPaySale.detail_next_attempt_at.is_(None),
                            FunPaySale.detail_next_attempt_at <= detail_now,
                        ),
                    )
                    .order_by(
                        FunPaySale.detail_attempts.asc(),
                        FunPaySale.detail_next_attempt_at.asc().nulls_first(),
                        FunPaySale.created_at.desc(),
                        FunPaySale.id.desc(),
                    )
                    .limit(1)
                )
                if newest is not None:
                    detail_candidates = [newest] + [
                        sale for sale in oldest if sale.id != newest.id
                    ][: queue_limit - 1]

        enriched = 0
        errors = 0
        for sale in detail_candidates[: max(0, detail_limit)]:
            try:
                info = await gateway.get_order(sale.funpay_order_id)
                if (
                    info.order_id != sale.funpay_order_id
                    or info.buyer_id <= 0
                    or str(info.buyer_id) != sale.buyer_funpay_id
                    or info.chat_id <= 0
                ):
                    raise InvalidSaleProvenanceError(
                        f"Order detail identity mismatch for {sale.funpay_order_id}"
                    )
            except Exception as exc:
                errors += 1
                self._defer_detail_retry(sale, detail_now)
                logger.info(
                    "FunPay sale enrichment deferred order=%s error=%s",
                    sale.funpay_order_id,
                    type(exc).__name__,
                )
                if self._is_global_funpay_error(exc):
                    self._defer_global_page_retry(sync_state, detail_now)
                    break
                continue
            self._apply_order_info(sale, info)
            sale.detail_attempts = 0
            sale.detail_next_attempt_at = None
            local_order = await session.scalar(
                select(Order).where(
                    Order.funpay_order_id == sale.funpay_order_id
                )
            )
            if local_order is not None:
                sale.order_id = local_order.id
            await session.flush()
            await self._verify_conversation(session, sale)
            sync_state.page_backoff_attempts = 0
            sync_state.page_backoff_until = None
            enriched += 1

        return SalesSyncResult(
            imported=imported,
            enriched=enriched,
            enrichment_errors=errors,
            history_errors=history_errors,
        )

    @staticmethod
    def _detail_retry_due(sale: FunPaySale, now: datetime) -> bool:
        retry_at = sale.detail_next_attempt_at
        if retry_at is None:
            return True
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return retry_at <= now

    @staticmethod
    def _defer_detail_retry(sale: FunPaySale, now: datetime) -> None:
        attempts = max(0, sale.detail_attempts) + 1
        exponent = min(12, attempts - 1)
        delay_seconds = min(
            _DETAIL_RETRY_BASE.total_seconds() * (2**exponent),
            _DETAIL_RETRY_MAX.total_seconds(),
        )
        sale.detail_attempts = attempts
        sale.detail_next_attempt_at = now + timedelta(seconds=delay_seconds)

    async def sync_order(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: str,
    ) -> SalesSyncResult:
        return await self.sync_recent_sales(
            session,
            gateway,
            limit=1,
            detail_limit=1,
            order_id=order_id,
        )

    async def refresh_buyer_profiles(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        *,
        limit: int = DEFAULT_PROFILE_BATCH,
        stale_after: timedelta = _PROFILE_REFRESH_TTL,
    ) -> ProfileRefreshResult:
        """Refresh a bounded, durable oldest-first queue of verified buyers."""

        if limit <= 0:
            return ProfileRefreshResult(refreshed=0, errors=0)
        now = _utcnow()
        state = await self._get_sync_state(session)
        if self._future(state.page_backoff_until, now):
            return ProfileRefreshResult(refreshed=0, errors=0)

        stale_before = now - max(stale_after, timedelta())
        verified_sale = exists().where(
            and_(
                FunPaySale.funpay_chat_id == ChatConversation.funpay_chat_id,
                FunPaySale.buyer_funpay_id == ChatConversation.buyer_funpay_id,
            )
        )
        candidates = list(
            (
                await session.execute(
                    select(ChatConversation)
                    .where(
                        ChatConversation.verified_sale.is_(True),
                        ChatConversation.buyer_funpay_id.is_not(None),
                        verified_sale,
                        or_(
                            ChatConversation.profile_checked_at.is_(None),
                            ChatConversation.profile_checked_at <= stale_before,
                        ),
                        or_(
                            ChatConversation.profile_next_attempt_at.is_(None),
                            ChatConversation.profile_next_attempt_at <= now,
                        ),
                    )
                    .order_by(
                        ChatConversation.profile_checked_at.asc().nulls_first(),
                        ChatConversation.id.asc(),
                    )
                    .limit(limit)
                )
            ).scalars()
        )

        refreshed = 0
        errors = 0
        attempted = False
        global_failure = False
        seen_buyer_ids: set[str] = set()
        for conversation in candidates:
            buyer_key = conversation.buyer_funpay_id or ""
            if buyer_key in seen_buyer_ids:
                continue
            seen_buyer_ids.add(buyer_key)
            attempted = True
            try:
                buyer_id = int(buyer_key)
                if buyer_id <= 0:
                    raise InvalidSaleProvenanceError("Invalid buyer profile ID")
                profile = await gateway.get_buyer_profile(buyer_id)
                profile = self._validated_profile(profile, buyer_id)
            except Exception as exc:
                errors += 1
                # Presence is volatile. A failed refresh must not leave an
                # old "online" or relative last-seen string looking current.
                self._defer_profile_retry(conversation, now)
                await session.execute(
                    update(ChatConversation)
                    .where(
                        ChatConversation.verified_sale.is_(True),
                        ChatConversation.buyer_funpay_id == buyer_key,
                    )
                    .values(
                        buyer_is_online=None,
                        buyer_status_text=None,
                        profile_attempts=conversation.profile_attempts,
                        profile_next_attempt_at=(
                            conversation.profile_next_attempt_at
                        ),
                    )
                )
                await session.execute(
                    update(FunPaySale)
                    .where(FunPaySale.buyer_funpay_id == buyer_key)
                    .values(
                        buyer_is_online=None,
                        buyer_status_text=None,
                        updated_at=now,
                    )
                )
                logger.info(
                    "FunPay buyer profile refresh deferred buyer=%s error=%s",
                    buyer_key,
                    type(exc).__name__,
                )
                if self._is_global_funpay_error(exc):
                    global_failure = True
                    self._defer_global_page_retry(state, now)
                    break
                continue

            checked_at = _utcnow()
            await session.execute(
                update(ChatConversation)
                .where(
                    ChatConversation.verified_sale.is_(True),
                    ChatConversation.buyer_funpay_id == buyer_key,
                )
                .values(
                    buyer_username=profile.username,
                    buyer_avatar_url=profile.avatar_url,
                    buyer_is_online=profile.is_online,
                    buyer_status_text=profile.status_text,
                    profile_checked_at=checked_at,
                    profile_attempts=0,
                    profile_next_attempt_at=None,
                )
            )
            await session.execute(
                update(FunPaySale)
                .where(FunPaySale.buyer_funpay_id == buyer_key)
                .values(
                    buyer_username=profile.username,
                    buyer_avatar_url=profile.avatar_url,
                    buyer_is_online=profile.is_online,
                    buyer_status_text=profile.status_text,
                    profile_checked_at=checked_at,
                    updated_at=checked_at,
                )
            )
            refreshed += 1
        if attempted and not global_failure:
            # Any non-global response proves that the shared transport is
            # usable again, even if this particular user page was malformed.
            state.page_backoff_attempts = 0
            state.page_backoff_until = None
        state.updated_at = _utcnow()
        await session.flush()
        return ProfileRefreshResult(refreshed=refreshed, errors=errors)

    @staticmethod
    def _validated_profile(
        profile: BuyerProfileInfo,
        expected_id: int,
    ) -> BuyerProfileInfo:
        if profile.buyer_id <= 0 or profile.buyer_id != expected_id:
            raise InvalidSaleProvenanceError(
                f"Buyer profile identity mismatch for {expected_id}"
            )
        username = SaleRegistryService._clean_profile_text(profile.username)
        if username is None:
            raise InvalidSaleProvenanceError(
                f"Buyer profile username missing for {expected_id}"
            )
        return BuyerProfileInfo(
            buyer_id=profile.buyer_id,
            username=username,
            avatar_url=SaleRegistryService._clean_profile_text(
                profile.avatar_url
            ),
            is_online=(
                bool(profile.is_online)
                if profile.is_online is not None
                else None
            ),
            status_text=SaleRegistryService._clean_profile_text(
                profile.status_text
            ),
        )

    @staticmethod
    def _clean_profile_text(value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @staticmethod
    def _future(value: datetime | None, now: datetime) -> bool:
        if value is None:
            return False
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value > now

    @staticmethod
    def _is_global_funpay_error(exc: Exception) -> bool:
        status = getattr(exc, "status", None)
        if status in {401, 403, 429}:
            return True
        if isinstance(status, int) and status >= 500:
            return True
        if type(exc).__name__ in {
            "RateLimitExceededError",
            "UnauthorizedError",
            "ForbiddenError",
            "FunPayServerError",
            "BotUnauthenticatedError",
        }:
            return True
        return isinstance(exc, (ConnectionError, TimeoutError, OSError)) or (
            type(exc).__module__.startswith("aiohttp.")
        )

    @staticmethod
    def _defer_profile_retry(
        conversation: ChatConversation,
        now: datetime,
    ) -> None:
        attempts = max(0, conversation.profile_attempts) + 1
        exponent = min(12, attempts - 1)
        delay_seconds = min(
            _PROFILE_RETRY_BASE.total_seconds() * (2**exponent),
            _PROFILE_RETRY_MAX.total_seconds(),
        )
        conversation.profile_attempts = attempts
        conversation.profile_next_attempt_at = now + timedelta(
            seconds=delay_seconds
        )

    @staticmethod
    def _defer_global_page_retry(
        state: FunPaySaleSyncState,
        now: datetime,
    ) -> None:
        attempts = max(0, state.page_backoff_attempts) + 1
        exponent = min(12, attempts - 1)
        delay_seconds = min(
            _PROFILE_RETRY_BASE.total_seconds() * (2**exponent),
            _PROFILE_RETRY_MAX.total_seconds(),
        )
        state.page_backoff_attempts = attempts
        state.page_backoff_until = now + timedelta(seconds=delay_seconds)

    @staticmethod
    async def _get_sync_state(
        session: AsyncSession,
    ) -> FunPaySaleSyncState:
        state = await session.scalar(
            select(FunPaySaleSyncState)
            .where(FunPaySaleSyncState.id == 1)
            .with_for_update()
        )
        if state is None:
            state = FunPaySaleSyncState(id=1)
            session.add(state)
            await session.flush()
        return state

    async def update_status(
        self,
        session: AsyncSession,
        order_id: str,
        status: SaleStatus | str,
    ) -> FunPaySale | None:
        sale = await self._get_by_remote_order(session, order_id)
        if sale is None:
            return None
        sale.status = _status_value(status)
        sale.updated_at = _utcnow()
        await session.flush()
        return sale

    async def ensure_conversation(
        self,
        session: AsyncSession,
        sale: FunPaySale,
        chat_id: int,
    ) -> ChatConversation | None:
        await self._learn_chat(session, sale, chat_id)
        return await self._verify_conversation(session, sale)

    async def resolve_message_sale(
        self,
        session: AsyncSession,
        message: MessageInfo,
    ) -> FunPaySale | None:
        """Authorize a message by exact sale, chat, then inbound buyer ID."""

        if message.chat_id <= 0:
            return None
        sender_id = str(message.sender_id) if message.sender_id is not None else None
        if message.order_id:
            sale = await self._get_by_remote_order(session, message.order_id)
            if sale is not None and self._sender_matches(sale, sender_id, message.from_me):
                if sale.funpay_chat_id != str(message.chat_id):
                    if message.from_me or sender_id != sale.buyer_funpay_id:
                        return None
                    if not await self._rebind_buyer_chat(
                        session, sale.buyer_funpay_id, str(message.chat_id)
                    ):
                        return None
                return sale

        sale = await session.scalar(
            select(FunPaySale)
            .where(FunPaySale.funpay_chat_id == str(message.chat_id))
            .order_by(FunPaySale.created_at.desc(), FunPaySale.id.desc())
        )
        if sale is not None and self._sender_matches(sale, sender_id, message.from_me):
            return sale

        if not message.from_me and sender_id is not None:
            sale = await session.scalar(
                select(FunPaySale)
                .where(FunPaySale.buyer_funpay_id == sender_id)
                .order_by(FunPaySale.created_at.desc(), FunPaySale.id.desc())
            )
            if sale is not None:
                # A plain /chat/?node= message often has no order meta. The
                # stable, verified sender ID is the authority when FunPay has
                # rotated the chat node. Merge the old inbox row before moving
                # every sale so one buyer is not split into two conversations.
                rebound = await self._rebind_buyer_chat(
                    session, sender_id, str(message.chat_id)
                )
                return sale if rebound else None
        return None

    async def _upsert_previews(
        self,
        session: AsyncSession,
        previews: list[SalePreviewInfo],
    ) -> tuple[list[FunPaySale], int]:
        valid: dict[str, SalePreviewInfo] = {}
        invalid_count = 0
        for preview in previews:
            if not preview.order_id or preview.buyer_id <= 0:
                invalid_count += 1
                continue
            valid.setdefault(preview.order_id, preview)
        if invalid_count:
            logger.warning(
                "Skipping %s sale previews with incomplete identity",
                invalid_count,
            )
        if not valid:
            return [], 0
        existing = list(
            (
                await session.execute(
                    select(FunPaySale).where(
                        FunPaySale.funpay_order_id.in_(list(valid))
                    )
                )
            ).scalars()
        )
        by_order = {sale.funpay_order_id: sale for sale in existing}
        accepted: list[FunPaySale] = []
        imported = 0
        now = _utcnow()
        for order_id, preview in valid.items():
            buyer_id = str(preview.buyer_id)
            sale = by_order.get(order_id)
            if sale is None:
                sale = FunPaySale(
                    funpay_order_id=order_id,
                    buyer_funpay_id=buyer_id,
                    status=_status_value(preview.status),
                    created_at=preview.created_at or now,
                )
                session.add(sale)
                by_order[order_id] = sale
                imported += 1
            elif sale.buyer_funpay_id != buyer_id:
                logger.error(
                    "Ignoring changed buyer identity for FunPay sale %s",
                    order_id,
                )
                continue
            sale.status = _status_value(preview.status)
            sale.buyer_username = preview.buyer_username or sale.buyer_username
            sale.buyer_avatar_url = preview.buyer_avatar_url
            sale.buyer_is_online = preview.buyer_is_online
            sale.buyer_status_text = preview.buyer_status_text
            sale.profile_checked_at = now
            sale.updated_at = now
            accepted.append(sale)
        await session.flush()
        return accepted, imported

    async def _reconcile_preview_conversations(
        self,
        session: AsyncSession,
        sales: list[FunPaySale],
    ) -> None:
        """Apply one batched profile/conversation update for a preview page."""

        if not sales:
            return
        latest_by_buyer: dict[str, FunPaySale] = {}
        latest_by_chat: dict[str, FunPaySale] = {}
        for sale in sales:
            current_buyer = latest_by_buyer.get(sale.buyer_funpay_id)
            if current_buyer is None or self._newer(sale, current_buyer):
                latest_by_buyer[sale.buyer_funpay_id] = sale
            if sale.funpay_chat_id is not None:
                current_chat = latest_by_chat.get(sale.funpay_chat_id)
                if current_chat is None or self._newer(sale, current_chat):
                    latest_by_chat[sale.funpay_chat_id] = sale

        buyer_ids = set(latest_by_buyer)
        chat_ids = set(latest_by_chat)
        conversations = list(
            (
                await session.execute(
                    select(ChatConversation).where(
                        (ChatConversation.buyer_funpay_id.in_(buyer_ids))
                        | (ChatConversation.funpay_chat_id.in_(chat_ids))
                    )
                )
            ).scalars()
        )
        by_chat = {item.funpay_chat_id: item for item in conversations}
        for chat_id, chat_sale in latest_by_chat.items():
            conversation = by_chat.get(chat_id)
            if conversation is None:
                conversation = ChatConversation(
                    funpay_chat_id=chat_id,
                    buyer_funpay_id=chat_sale.buyer_funpay_id,
                )
                session.add(conversation)
                by_chat[chat_id] = conversation
                conversations.append(conversation)
            elif conversation.buyer_funpay_id not in (
                None,
                chat_sale.buyer_funpay_id,
            ):
                logger.error(
                    "Refusing to verify chat %s for conflicting buyers %s/%s",
                    chat_id,
                    conversation.buyer_funpay_id,
                    chat_sale.buyer_funpay_id,
                )
                continue
            profile_sale = latest_by_buyer[chat_sale.buyer_funpay_id]
            self._apply_sale_to_conversation(
                conversation,
                chat_sale,
                profile_sale=profile_sale,
            )

        # A newer preview may not yet have an enriched chat page. It still has
        # the freshest authoritative online/last-seen profile for an already
        # verified buyer conversation.
        for conversation in conversations:
            if not conversation.verified_sale or conversation.buyer_funpay_id is None:
                continue
            profile_sale = latest_by_buyer.get(conversation.buyer_funpay_id)
            if profile_sale is not None:
                self._copy_profile(conversation, profile_sale)
        await session.flush()

    async def _verify_conversation(
        self,
        session: AsyncSession,
        sale: FunPaySale,
    ) -> ChatConversation | None:
        if sale.funpay_chat_id is None:
            return None
        conversation = await session.scalar(
            select(ChatConversation).where(
                ChatConversation.funpay_chat_id == sale.funpay_chat_id
            )
        )
        if conversation is None:
            conversation = ChatConversation(
                funpay_chat_id=sale.funpay_chat_id,
                buyer_funpay_id=sale.buyer_funpay_id,
            )
            session.add(conversation)
            await session.flush()
        elif (
            conversation.buyer_funpay_id is not None
            and conversation.buyer_funpay_id != sale.buyer_funpay_id
        ):
            logger.error(
                "Refusing to verify chat %s for conflicting buyers %s/%s",
                sale.funpay_chat_id,
                conversation.buyer_funpay_id,
                sale.buyer_funpay_id,
            )
            return None

        conversation.verified_sale = True
        conversation.buyer_funpay_id = sale.buyer_funpay_id
        self._copy_profile(conversation, sale)
        if await self._is_newest_sale_for_conversation(session, conversation, sale):
            conversation.funpay_order_id = sale.funpay_order_id
            conversation.order_id = sale.order_id
        await session.flush()
        return conversation

    @classmethod
    def _apply_sale_to_conversation(
        cls,
        conversation: ChatConversation,
        sale: FunPaySale,
        *,
        profile_sale: FunPaySale | None = None,
    ) -> None:
        conversation.verified_sale = True
        conversation.buyer_funpay_id = sale.buyer_funpay_id
        conversation.funpay_order_id = sale.funpay_order_id
        conversation.order_id = sale.order_id
        cls._copy_profile(conversation, profile_sale or sale)

    @staticmethod
    def _copy_profile(
        conversation: ChatConversation,
        sale: FunPaySale,
    ) -> None:
        previous_checked_at = conversation.profile_checked_at
        authoritative = SaleRegistryService._is_later_profile_check(
            sale.profile_checked_at,
            previous_checked_at,
        )
        conversation.buyer_username = sale.buyer_username or conversation.buyer_username
        if authoritative:
            conversation.buyer_avatar_url = sale.buyer_avatar_url
            conversation.buyer_is_online = sale.buyer_is_online
            conversation.buyer_status_text = sale.buyer_status_text
        elif sale.buyer_avatar_url is not None:
            conversation.buyer_avatar_url = sale.buyer_avatar_url
        conversation.profile_checked_at = (
            sale.profile_checked_at or conversation.profile_checked_at
        )
        if authoritative:
            # Head previews and freshly parsed order pages are successful
            # current-profile sources and supersede any older retry state.
            conversation.profile_attempts = 0
            conversation.profile_next_attempt_at = None

    @staticmethod
    def _is_later_profile_check(
        candidate: datetime | None,
        current: datetime | None,
    ) -> bool:
        if candidate is None:
            return False
        if current is None:
            return True
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return candidate > current

    @staticmethod
    def _newer(left: FunPaySale, right: FunPaySale) -> bool:
        left_at = left.created_at
        right_at = right.created_at
        if left_at.tzinfo is None:
            left_at = left_at.replace(tzinfo=timezone.utc)
        if right_at.tzinfo is None:
            right_at = right_at.replace(tzinfo=timezone.utc)
        return (left_at, left.id or 0) > (right_at, right.id or 0)

    async def _is_newest_sale_for_conversation(
        self,
        session: AsyncSession,
        conversation: ChatConversation,
        candidate: FunPaySale,
    ) -> bool:
        if conversation.funpay_order_id in (None, candidate.funpay_order_id):
            return True
        current = await self._get_by_remote_order(
            session, conversation.funpay_order_id
        )
        if current is None:
            return True
        candidate_at = candidate.created_at
        current_at = current.created_at
        if candidate_at.tzinfo is None:
            candidate_at = candidate_at.replace(tzinfo=timezone.utc)
        if current_at.tzinfo is None:
            current_at = current_at.replace(tzinfo=timezone.utc)
        return candidate_at >= current_at

    @staticmethod
    def _apply_order_info(sale: FunPaySale, info: OrderInfo) -> None:
        now = _utcnow()
        sale.funpay_chat_id = str(info.chat_id)
        sale.buyer_funpay_id = str(info.buyer_id)
        sale.status = _status_value(info.status)
        sale.buyer_username = info.buyer_username or sale.buyer_username
        has_profile = any(
            value is not None
            for value in (
                info.buyer_username,
                info.buyer_avatar_url,
                info.buyer_is_online,
                info.buyer_status_text,
            )
        )
        if has_profile:
            sale.buyer_avatar_url = info.buyer_avatar_url
            sale.buyer_is_online = info.buyer_is_online
            sale.buyer_status_text = info.buyer_status_text
            sale.profile_checked_at = now
        sale.updated_at = now

    @staticmethod
    def _sender_matches(
        sale: FunPaySale,
        sender_id: str | None,
        from_me: bool,
    ) -> bool:
        return from_me or sender_id is None or sender_id == sale.buyer_funpay_id

    @staticmethod
    async def _learn_chat(
        session: AsyncSession,
        sale: FunPaySale,
        chat_id: int,
    ) -> None:
        if chat_id <= 0:
            raise InvalidSaleProvenanceError("Invalid FunPay chat id")
        if sale.funpay_chat_id is None:
            sale.funpay_chat_id = str(chat_id)
            await session.flush()

    async def _rebind_buyer_chat(
        self,
        session: AsyncSession,
        buyer_id: str,
        chat_id: str,
    ) -> bool:
        conversations = list(
            (
                await session.execute(
                    select(ChatConversation)
                    .where(
                        or_(
                            ChatConversation.funpay_chat_id == chat_id,
                            ChatConversation.buyer_funpay_id == buyer_id,
                        )
                    )
                    .order_by(
                        ChatConversation.last_message_at.desc().nulls_last(),
                        ChatConversation.id.desc(),
                    )
                )
            ).scalars()
        )
        current = next(
            (item for item in conversations if item.funpay_chat_id == chat_id),
            None,
        )
        if current is not None and current.buyer_funpay_id not in (None, buyer_id):
            logger.error(
                "Refusing to rebind chat %s from buyer %s to %s",
                chat_id,
                current.buyer_funpay_id,
                buyer_id,
            )
            return False

        target = current or next(
            (item for item in conversations if item.buyer_funpay_id == buyer_id),
            None,
        )
        if target is not None and target.funpay_chat_id != chat_id:
            target.funpay_chat_id = chat_id
            await session.flush()

        if target is not None:
            target.buyer_funpay_id = buyer_id
            target.verified_sale = True
            others = [
                item
                for item in conversations
                if item.id != target.id and item.buyer_funpay_id == buyer_id
            ]
            existing_source_ids = set(
                (
                    await session.execute(
                        select(ChatMessage.funpay_message_id).where(
                            ChatMessage.conversation_id == target.id,
                            ChatMessage.funpay_message_id.is_not(None),
                        )
                    )
                ).scalars()
            )
            for old in others:
                old_messages = list(
                    (
                        await session.execute(
                            select(ChatMessage).where(
                                ChatMessage.conversation_id == old.id
                            )
                        )
                    ).scalars()
                )
                for item in old_messages:
                    if (
                        item.funpay_message_id is not None
                        and item.funpay_message_id in existing_source_ids
                    ):
                        await session.delete(item)
                        continue
                    item.conversation_id = target.id
                    if item.funpay_message_id is not None:
                        existing_source_ids.add(item.funpay_message_id)
                await session.flush()
                await session.delete(old)

            await session.flush()
            messages = list(
                (
                    await session.execute(
                        select(ChatMessage)
                        .where(ChatMessage.conversation_id == target.id)
                        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                    )
                ).scalars()
            )
            target.unread_count = sum(
                1
                for item in messages
                if item.direction == "incoming" and not item.is_read
            )
            if messages:
                latest = messages[0]
                target.last_message_text = latest.text
                target.last_message_direction = latest.direction
                target.last_message_at = latest.created_at
            else:
                target.last_message_text = None
                target.last_message_direction = None
                target.last_message_at = None

        sales = list(
            (
                await session.execute(
                    select(FunPaySale).where(FunPaySale.buyer_funpay_id == buyer_id)
                )
            ).scalars()
        )
        for sale in sales:
            sale.funpay_chat_id = chat_id
        local_orders = list(
            (
                await session.execute(
                    select(Order).where(Order.buyer_funpay_id == buyer_id)
                )
            ).scalars()
        )
        for order in local_orders:
            order.funpay_chat_id = chat_id
        rentals = list(
            (
                await session.execute(
                    select(Rental)
                    .join(Order, Order.id == Rental.order_id)
                    .where(Order.buyer_funpay_id == buyer_id)
                )
            ).scalars()
        )
        for rental in rentals:
            rental.buyer_funpay_chat_id = chat_id
        await session.flush()
        return True

    @staticmethod
    async def _get_by_remote_order(
        session: AsyncSession,
        order_id: str,
    ) -> FunPaySale | None:
        return await session.scalar(
            select(FunPaySale).where(FunPaySale.funpay_order_id == order_id)
        )
