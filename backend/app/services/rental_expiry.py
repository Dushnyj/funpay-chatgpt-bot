from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import CheckJobQueue
from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account
from app.models.audit import AuditLog
from app.models.catalog import Duration, SubscriptionTier
from app.models.rental import Order, Rental
from app.services.kick_service import KickService
from app.services.messages import render_message
from app.services.durations import (
    format_duration,
    format_legacy_days,
    format_remaining_seconds,
)


EXPIRY_MESSAGE_TIMEOUT_SECONDS = 30.0


class RentalExpiryService:
    """Expire rentals and revoke the corresponding OpenAI sessions."""

    def __init__(
        self,
        kick_service: KickService | None = None,
        job_queue: CheckJobQueue | None = None,
    ) -> None:
        self._kick = kick_service or KickService()
        self._jobs = job_queue or CheckJobQueue()

    async def expire_overdue(
        self,
        session: AsyncSession,
        gateway: ChatGateway | None,
    ) -> list[Rental]:
        """Помечает все active аренды с expires_at <= NOW() как expired.

        Отправляет expiry_message в чат каждой истёкшей аренды.
        Возвращает список обработанных Rental.
        """
        candidates = await self.prepare_overdue_batch(session)
        processed: list[Rental] = []
        for rental_id, order_id in candidates:
            rental = await self.expire_candidate(
                session,
                gateway,
                rental_id=rental_id,
                order_id=order_id,
            )
            if rental is not None:
                processed.append(rental)
        if gateway is not None:
            for rental_id, order_id in (
                await self.pending_notification_candidates(session)
            ):
                await self.notify_expiration_candidate(
                    session,
                    gateway,
                    rental_id=rental_id,
                    order_id=order_id,
                )
        return processed

    async def prepare_overdue_batch(
        self,
        session: AsyncSession,
    ) -> list[tuple[int, int]]:
        """Return a stable candidate snapshot without holding row locks.

        The scheduler may process each pair in its own ``AsyncSession``. The
        authoritative state transition still happens in ``expire_candidate``
        through the existing Order -> Rental -> Account durable claim.
        """

        now = datetime.now(timezone.utc)
        await self._release_stale_replacement_reservations(session, now=now)
        candidate_result = await session.execute(
            select(Rental.id, Rental.order_id)
            .where(
                Rental.status.in_(["active", "expiry_pending"]),
                Rental.expires_at <= now,
                # Initial rental time starts only after credentials are
                # delivered. A 30-minute offer must not expire while delivery
                # is still retrying. Replacement retries keep the original
                # expiry and therefore remain eligible here.
                or_(
                    Rental.credentials_delivery_template != "welcome",
                    Rental.credentials_delivery_status == "sent",
                    Rental.credentials_delivery_attempts > 0,
                ),
            )
            .order_by(Rental.order_id, Rental.id)
        )
        return [
            (int(rental_id), int(order_id))
            for rental_id, order_id in candidate_result.all()
        ]

    async def expire_candidate(
        self,
        session: AsyncSession,
        gateway: ChatGateway | None,
        *,
        rental_id: int,
        order_id: int,
    ) -> Rental | None:
        """Claim, revoke, and finalize exactly one overdue rental.

        Callers must provide a session that is not shared with another
        concurrent candidate. No row lock survives the external kick.
        """

        claimed = await self._claim_overdue(
            session,
            rental_id=rental_id,
            order_id=order_id,
            now=datetime.now(timezone.utc),
        )
        if claimed is None:
            return None
        rental, _newly_overdue = claimed
        account_id = rental.account_id
        claim_started_at = rental.expiry_revoke_started_at

        # No row lock is held during browser/FunPay I/O. The durable
        # expiry_pending claim and maintenance account state already stop
        # allocation and buyer authorization.
        revoked = await self._revoke_account(session, gateway, account_id)
        await session.commit()
        finalized = await self._finalize_revoke(
            session,
            rental_id=rental_id,
            order_id=order_id,
            account_id=account_id,
            claim_started_at=claim_started_at,
            success=revoked,
        )
        return finalized or rental

    async def pending_notification_candidates(
        self,
        session: AsyncSession,
    ) -> list[tuple[int, int]]:
        """Return terminal messages left durable across a process crash."""

        candidates = await session.execute(
            select(Rental.id, Rental.order_id)
            .where(
                Rental.status == "expired",
                Rental.expiry_notified_at.is_(None),
            )
            .order_by(Rental.order_id, Rental.id)
        )
        return [
            (int(rental_id), int(order_id))
            for rental_id, order_id in candidates.all()
        ]

    async def notify_expiration_candidate(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        *,
        rental_id: int,
        order_id: int,
    ) -> None:
        """Claim and deliver one terminal expiry notification at least once."""

        claimed = await self._claim_expiry_notification(
            session,
            rental_id=rental_id,
            order_id=order_id,
        )
        if claimed is None:
            return
        rental, claim_started_at = claimed
        notification_chat_id = int(rental.buyer_funpay_chat_id)
        success = False
        error: str | None = None
        try:
            text = await self._render_expiry(session, rental)
            # Template/catalog reads are complete. Never cancel a database
            # operation merely because FunPay message delivery timed out.
            await session.commit()
            await asyncio.wait_for(
                gateway.send_message(
                    chat_id=notification_chat_id,
                    text=text,
                ),
                timeout=EXPIRY_MESSAGE_TIMEOUT_SECONDS,
            )
            success = True
        except Exception as exc:
            error = str(exc)
            if session.in_transaction():
                await session.rollback()
        await self._finalize_expiry_notification(
            session,
            rental_id=rental_id,
            order_id=order_id,
            claim_started_at=claim_started_at,
            success=success,
            error=error,
        )

    async def _release_stale_replacement_reservations(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> None:
        """Free abandoned targets without guessing whether logout succeeded."""

        cutoff = now - timedelta(minutes=5)
        candidates = await session.execute(
            select(
                Rental.id,
                Rental.order_id,
                Rental.expiry_revoke_started_at,
                Rental.replacement_target_account_id,
            )
            .where(
                Rental.replacement_target_account_id.is_not(None),
                or_(
                    Rental.expiry_revoke_started_at.is_(None),
                    Rental.expiry_revoke_started_at <= cutoff,
                ),
            )
            .order_by(Rental.order_id, Rental.id)
        )
        for (
            rental_id,
            order_id,
            observed_claim,
            observed_target_id,
        ) in candidates.all():
            order = (
                await session.execute(
                    select(Order)
                    .where(Order.id == order_id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if order is None:
                await session.commit()
                continue
            rental = (
                await session.execute(
                    select(Rental)
                    .where(Rental.id == rental_id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if rental is None:
                await session.commit()
                continue
            stored_claim = rental.expiry_revoke_started_at
            if stored_claim is not None and stored_claim.tzinfo is None:
                stored_claim = stored_claim.replace(tzinfo=timezone.utc)
            if observed_claim is not None and observed_claim.tzinfo is None:
                observed_claim = observed_claim.replace(tzinfo=timezone.utc)
            owns_observation = (
                rental.order_id == order.id
                and rental.replacement_target_account_id
                == observed_target_id
                and stored_claim == observed_claim
            )
            claim_is_live = (
                stored_claim is not None and stored_claim > cutoff
            )
            if not owns_observation or claim_is_live:
                await session.commit()
                continue
            rental.expiry_revoke_started_at = None
            rental.replacement_target_account_id = None
            session.add(AuditLog(
                event_type="replacement_stale_reservation_released_scheduled",
                account_id=rental.account_id,
                order_id=order.id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={"target_account_id": observed_target_id},
            ))
            # The old account deliberately remains in maintenance: after a
            # process crash we cannot know whether external logout completed.
            await session.commit()

    async def _claim_expiry_notification(
        self,
        session: AsyncSession,
        *,
        rental_id: int,
        order_id: int,
    ) -> tuple[Rental, datetime] | None:
        now = datetime.now(timezone.utc)
        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if order is None:
            await session.commit()
            return None
        rental = (
            await session.execute(
                select(Rental)
                .where(Rental.id == rental_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            rental is None
            or rental.order_id != order.id
            or rental.status != "expired"
            or rental.expiry_notified_at is not None
        ):
            await session.commit()
            return None
        # A refund supersedes the expiry notification. Mark it handled so an
        # old terminal message cannot be emitted after the refund callback.
        if order.status in {"refund_pending", "refunded"}:
            rental.expiry_notified_at = now
            session.add(AuditLog(
                event_type="expiry_message_suppressed_refund",
                account_id=rental.account_id,
                order_id=order.id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
            ))
            await session.commit()
            return None
        claim_started = rental.expiry_revoke_started_at
        if claim_started is not None and claim_started.tzinfo is None:
            claim_started = claim_started.replace(tzinfo=timezone.utc)
        if (
            claim_started is not None
            and claim_started > now - timedelta(minutes=5)
        ):
            await session.commit()
            return None
        rental.expiry_revoke_started_at = now
        await session.commit()
        return rental, now

    async def _finalize_expiry_notification(
        self,
        session: AsyncSession,
        *,
        rental_id: int,
        order_id: int,
        claim_started_at: datetime,
        success: bool,
        error: str | None,
    ) -> None:
        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if order is None:
            await session.commit()
            return
        rental = (
            await session.execute(
                select(Rental)
                .where(Rental.id == rental_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        stored_claim = rental.expiry_revoke_started_at if rental else None
        if stored_claim is not None and stored_claim.tzinfo is None:
            stored_claim = stored_claim.replace(tzinfo=timezone.utc)
        if claim_started_at.tzinfo is None:
            claim_started_at = claim_started_at.replace(tzinfo=timezone.utc)
        if (
            rental is None
            or rental.order_id != order.id
            or stored_claim != claim_started_at
        ):
            await session.commit()
            return
        rental.expiry_revoke_started_at = None
        if success:
            rental.expiry_notified_at = datetime.now(timezone.utc)
        else:
            session.add(AuditLog(
                event_type="expiry_message_failed",
                account_id=rental.account_id,
                order_id=order.id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={"error": error or "unknown"},
            ))
        await session.commit()

    async def _claim_overdue(
        self,
        session: AsyncSession,
        *,
        rental_id: int,
        order_id: int,
        now: datetime,
    ) -> tuple[Rental, bool] | None:
        """Short Order -> Rental -> Account claim; commits before I/O."""

        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if order is None or order.status in {"refund_pending", "refunded"}:
            await session.commit()
            return None
        rental = (
            await session.execute(
                select(Rental)
                .where(Rental.id == rental_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if rental is None:
            await session.commit()
            return None
        expires_at = rental.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        claim_started = rental.expiry_revoke_started_at
        if claim_started is not None and claim_started.tzinfo is None:
            claim_started = claim_started.replace(tzinfo=timezone.utc)
        claim_is_live = (
            claim_started is not None
            and claim_started > now - timedelta(minutes=5)
        )
        initial_delivery_pending = (
            rental.credentials_delivery_template == "welcome"
            and rental.credentials_delivery_status != "sent"
            and rental.credentials_delivery_attempts == 0
        )
        if (
            rental.order_id != order.id
            or rental.status not in {"active", "expiry_pending"}
            or expires_at > now
            or initial_delivery_pending
            or claim_is_live
        ):
            await session.commit()
            return None
        newly_overdue = rental.status == "active"
        # A stale replacement claim must not reserve a healthy target forever.
        # Order -> Rental is locked and the shared revoke lease is no longer
        # live, so this worker may safely release the abandoned reservation.
        rental.replacement_target_account_id = None
        rental.status = "expiry_pending"
        rental.expiry_revoke_started_at = now
        account = (
            await session.execute(
                select(Account)
                .where(Account.id == rental.account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if account is not None:
            account.status = "maintenance"
        await session.commit()
        return rental, newly_overdue

    async def _finalize_revoke(
        self,
        session: AsyncSession,
        *,
        rental_id: int,
        order_id: int,
        account_id: int,
        claim_started_at: datetime | None,
        success: bool,
    ) -> Rental | None:
        """Short finalization with the same global lock order."""

        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if order is None:
            await session.commit()
            return None
        rental = (
            await session.execute(
                select(Rental)
                .where(Rental.id == rental_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        stored_claim = rental.expiry_revoke_started_at if rental else None
        if stored_claim is not None and stored_claim.tzinfo is None:
            stored_claim = stored_claim.replace(tzinfo=timezone.utc)
        if claim_started_at is not None and claim_started_at.tzinfo is None:
            claim_started_at = claim_started_at.replace(tzinfo=timezone.utc)
        owns_claim = stored_claim == claim_started_at
        if (
            rental is not None
            and rental.order_id == order.id
            and order.status in {"refund_pending", "refunded"}
        ):
            # Refund processing owns account revocation from this point.  A
            # successful expiry kick that finishes after the refund claim
            # must not overwrite the refund state or emit an expiry message.
            if owns_claim:
                rental.expiry_revoke_started_at = None
            await session.commit()
            return rental
        if (
            rental is None
            or rental.order_id != order.id
            or rental.account_id != account_id
            or rental.status != "expiry_pending"
            or not owns_claim
        ):
            await session.commit()
            return rental
        rental.expiry_revoke_started_at = None
        if success:
            rental.status = "expired"
        await session.commit()
        return rental

    async def _revoke_account(
        self,
        session: AsyncSession,
        gateway: ChatGateway | None,
        account_id: int,
    ) -> bool:
        account = await session.get(Account, account_id)
        if account is not None:
            account.status = "maintenance"
        try:
            result = await self._kick.kick(session, account_id)
        except Exception as exc:
            # Advisory-lock acquisition or a database-backed email provider
            # may leave this short kick transaction failed. The durable expiry
            # claim was committed earlier, so rolling back here is safe and
            # lets us persist a retryable failure audit below.
            await session.rollback()
            result = None
            error = str(exc)
        else:
            error = result.error

        success = bool(result and result.success)
        session.add(AuditLog(
            event_type="rental_expiry_kick",
            account_id=account_id,
            metadata_={
                "success": success,
                "deduplicated": bool(result and result.deduplicated),
                "error": error,
            },
        ))
        if not success:
            return False

        await self._jobs.enqueue(
            session,
            account_id=account_id,
            priority="refresh_recover",
            job_type="refresh_recover",
        )
        if gateway is None:
            return True

        active = await session.execute(
            select(Rental).where(
                Rental.account_id == account_id,
                Rental.status == "active",
            )
        )
        for rental in active.scalars().all():
            try:
                expires_at = rental.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                seconds = (
                    expires_at - datetime.now(timezone.utc)
                ).total_seconds()
                expires_in = format_remaining_seconds(seconds, rental.lang)
                text = await render_message(
                    session,
                    "disconnect",
                    rental.lang,
                    expires_in=expires_in,
                )
                await gateway.send_message(
                    chat_id=int(rental.buyer_funpay_chat_id), text=text,
                )
            except Exception as exc:
                session.add(AuditLog(
                    event_type="disconnect_message_failed",
                    account_id=account_id,
                    rental_id=rental.id,
                    chat_id=rental.buyer_funpay_chat_id,
                    metadata_={"error": str(exc)},
                ))
        return True

    async def _render_expiry(
        self,
        session: AsyncSession,
        rental: Rental,
    ) -> str:
        tier = await session.get(SubscriptionTier, rental.tier_id)
        duration = await session.get(Duration, rental.duration_id)
        text = await render_message(
            session, "expiry", rental.lang,
            tier=tier.name if tier else "",
            duration=(
                format_duration(duration.minutes, rental.lang)
                if duration else "—"
            ),
            duration_minutes=duration.minutes if duration else 0,
            days=format_legacy_days(duration.minutes) if duration else "0",
        )
        return text
