from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.audit import AuditLog
from app.models.rental import Order
from app.services.messages import render_message
from app.services.order_provenance import is_verified_bot_sale_order


BUYER_ORDER_CONFIRMED_EVENT = "buyer_order_confirmed_sent"
BUYER_ORDER_CONFIRMED_DUE_EVENT = "buyer_order_confirmed_due"
BUYER_ORDER_CONFIRMED_REQUEUED_EVENT = "buyer_order_confirmed_manual_requeued"
ORDER_NOTIFICATION_TIMEOUT_SECONDS = 10.0
ORDER_NOTIFICATION_MAX_ATTEMPTS = 12
_ORDER_NOTIFICATION_RETRY_BASE = timedelta(minutes=1)
_ORDER_NOTIFICATION_RETRY_MAX = timedelta(hours=24)


class GlobalOrderNotificationDeliveryError(RuntimeError):
    """Signal that the shared FunPay delivery channel is unavailable.

    The original exception is retained as ``cause`` and through exception
    chaining.  Callers handling a single live callback may log it like any
    other delivery failure, while the scheduled batch worker can stop before
    subjecting every queued order to the same outage and timeout.
    """

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(
            f"Shared FunPay notification delivery failed: {type(cause).__name__}"
        )


def buyer_confirmation_missing(order=Order):
    """Correlated proof that a completed Order has no sent audit marker."""

    return ~exists().where(
        AuditLog.event_type == BUYER_ORDER_CONFIRMED_EVENT,
        AuditLog.order_id == order.id,
    )


def buyer_confirmation_due(order=Order):
    """Correlated proof that this exact confirmation was scheduled."""

    return exists().where(
        AuditLog.event_type == BUYER_ORDER_CONFIRMED_DUE_EVENT,
        AuditLog.order_id == order.id,
    )


class OrderNotificationService:
    """Deliver retryable, idempotent non-secret order notifications."""

    async def mark_confirmed_due(
        self,
        session: AsyncSession,
        order: Order,
    ) -> bool:
        """Persist retry intent in the same transaction as completion."""

        if (
            order.id is None
            or order.status != "completed"
            or not await is_verified_bot_sale_order(session, order)
        ):
            return False
        existing = await session.execute(
            select(AuditLog.event_type)
            .where(
                AuditLog.event_type.in_((
                    BUYER_ORDER_CONFIRMED_DUE_EVENT,
                    BUYER_ORDER_CONFIRMED_EVENT,
                )),
                AuditLog.order_id == order.id,
            )
            .limit(1)
        )
        existing_event = existing.scalar_one_or_none()
        if existing_event == BUYER_ORDER_CONFIRMED_EVENT:
            order.confirmation_delivery_status = "sent"
            order.confirmation_delivery_next_attempt_at = None
            order.confirmation_delivery_last_error = None
            await session.flush()
            return False
        if existing_event == BUYER_ORDER_CONFIRMED_DUE_EVENT:
            if order.confirmation_delivery_status == "idle":
                order.confirmation_delivery_status = "pending"
                await session.flush()
            return False
        order.confirmation_delivery_status = "pending"
        order.confirmation_delivery_attempts = 0
        order.confirmation_delivery_next_attempt_at = None
        order.confirmation_delivery_last_error = None
        session.add(AuditLog(
            event_type=BUYER_ORDER_CONFIRMED_DUE_EVENT,
            order_id=order.id,
            chat_id=order.funpay_chat_id,
            metadata_={"template": "order_confirmed"},
        ))
        await session.flush()
        return True

    async def notify_confirmed(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: int,
    ) -> bool:
        # The Order lock serializes duplicate callbacks and the scheduled
        # retry worker across processes. A remote-send/DB-commit crash can
        # still cause an at-least-once duplicate, which FunPay cannot prevent
        # because its send endpoint has no idempotency key.
        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            order is None
            or order.status != "completed"
            or not await is_verified_bot_sale_order(session, order)
        ):
            await session.commit()
            return False
        already_sent = await session.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.event_type == BUYER_ORDER_CONFIRMED_EVENT,
                AuditLog.order_id == order.id,
            )
            .limit(1)
        )
        if already_sent is not None:
            order.confirmation_delivery_status = "sent"
            order.confirmation_delivery_next_attempt_at = None
            order.confirmation_delivery_last_error = None
            await session.commit()
            return False

        due = await session.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.event_type == BUYER_ORDER_CONFIRMED_DUE_EVENT,
                AuditLog.order_id == order.id,
            )
            .limit(1)
        )
        if due is None:
            # Defensive direct callers still get durable retry intent. Commit
            # it before remote I/O, then reacquire the Order lock so duplicate
            # callbacks cannot send concurrently in PostgreSQL.
            await self.mark_confirmed_due(session, order)
            await session.commit()
            order = (
                await session.execute(
                    select(Order)
                    .where(Order.id == order_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                order is None
                or order.status != "completed"
                or not await is_verified_bot_sale_order(session, order)
            ):
                await session.commit()
                return False
            already_sent = await session.scalar(
                select(AuditLog.id)
                .where(
                    AuditLog.event_type == BUYER_ORDER_CONFIRMED_EVENT,
                    AuditLog.order_id == order.id,
                )
                .limit(1)
            )
            if already_sent is not None:
                order.confirmation_delivery_status = "sent"
                order.confirmation_delivery_next_attempt_at = None
                order.confirmation_delivery_last_error = None
                await session.commit()
                return False

        if order.confirmation_delivery_status in {"manual", "sent"}:
            await session.commit()
            return False
        retry_at = order.confirmation_delivery_next_attempt_at
        if retry_at is not None:
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            if retry_at > datetime.now(timezone.utc):
                await session.commit()
                return False

        try:
            text = await render_message(
                session,
                "order_confirmed",
                order.buyer_locale,
            )
            await asyncio.wait_for(
                gateway.send_message(
                    chat_id=int(order.funpay_chat_id),
                    text=text,
                ),
                timeout=ORDER_NOTIFICATION_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            global_error = self._is_global_delivery_error(exc)
            await self._record_failure(session, order_id, exc)
            if global_error:
                raise GlobalOrderNotificationDeliveryError(exc) from exc
            raise
        session.add(AuditLog(
            event_type=BUYER_ORDER_CONFIRMED_EVENT,
            order_id=order.id,
            chat_id=order.funpay_chat_id,
            metadata_={"template": "order_confirmed"},
        ))
        order.confirmation_delivery_status = "sent"
        order.confirmation_delivery_next_attempt_at = None
        order.confirmation_delivery_last_error = None
        await session.commit()
        return True

    async def _record_failure(
        self,
        session: AsyncSession,
        order_id: int,
        exc: Exception,
    ) -> None:
        """Persist bounded per-order backoff after any render/send failure."""

        await session.rollback()
        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            order is None
            or order.status != "completed"
            or not await is_verified_bot_sale_order(session, order)
        ):
            await session.commit()
            return
        sent = await session.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.event_type == BUYER_ORDER_CONFIRMED_EVENT,
                AuditLog.order_id == order.id,
            )
            .limit(1)
        )
        if sent is not None:
            order.confirmation_delivery_status = "sent"
            order.confirmation_delivery_next_attempt_at = None
            order.confirmation_delivery_last_error = None
            await session.commit()
            return

        attempts = min(
            2_000_000_000,
            max(0, order.confirmation_delivery_attempts) + 1,
        )
        global_error = self._is_global_delivery_error(exc)
        order.confirmation_delivery_attempts = attempts
        order.confirmation_delivery_last_error = type(exc).__name__[:128]
        if attempts >= ORDER_NOTIFICATION_MAX_ATTEMPTS and not global_error:
            order.confirmation_delivery_status = "manual"
            order.confirmation_delivery_next_attempt_at = None
        else:
            exponent = min(12, attempts - 1)
            delay_seconds = min(
                _ORDER_NOTIFICATION_RETRY_BASE.total_seconds() * (2**exponent),
                _ORDER_NOTIFICATION_RETRY_MAX.total_seconds(),
            )
            order.confirmation_delivery_status = "failed"
            order.confirmation_delivery_next_attempt_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=delay_seconds)
            )
        await session.commit()

    @staticmethod
    def _is_global_delivery_error(exc: Exception) -> bool:
        """Separate shared FunPay outages from permanent per-chat poison."""

        status = getattr(exc, "status", None)
        if status in {401, 429}:
            return True
        if isinstance(status, int) and status >= 500:
            return True
        if type(exc).__name__ in {
            "RateLimitExceededError",
            "UnauthorizedError",
            "FunPayServerError",
            "BotUnauthenticatedError",
        }:
            return True
        return isinstance(exc, (ConnectionError, TimeoutError, OSError)) or (
            type(exc).__module__.startswith("aiohttp.")
        )
