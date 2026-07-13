from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.audit import AuditLog
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings
from app.services.account_pool import (
    AccountCriteria,
    AccountPool,
    limits_freshness_for_duration,
)
from app.services.delivery_policy import (
    CREDENTIAL_DELIVERY_MAX_ATTEMPTS,
    CREDENTIAL_SEND_TIMEOUT_SECONDS,
    DELIVERY_ALLOCATION_HEADROOM,
    credential_delivery_retry_delay_seconds,
)
from app.services.durations import (
    format_access_expiry,
    format_duration,
    format_legacy_days,
    format_plan_expiry,
    format_remaining_seconds,
)
from app.services.limit_eligibility import apply_limit_scope_filters
from app.services.messages import issued_usage_template_variables, render_message
from app.services.order_provenance import is_verified_bot_sale_order


CREDENTIAL_DELIVERY_LEASE = timedelta(minutes=5)
INITIAL_DELIVERY_SUBSCRIPTION_HEADROOM = DELIVERY_ALLOCATION_HEADROOM
REPLACEMENT_DELIVERY_MIN_REMAINING = timedelta(minutes=2)
_FULFILLABLE_ORDER_STATUSES = {"pending", "completed"}


class UnverifiedOrderProvenanceError(RuntimeError):
    """Credential delivery was requested for a non-bot or legacy order."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RentalService:
    """Связывает Order → Account → Rental → welcome message.

    fulfill_order: идемпотентен (если Rental для Order уже есть — возвращает существующий).
    Если аккаунт не найден — отправляет no_account_available, возвращает None.
    """

    def __init__(self, account_pool: AccountPool | None = None) -> None:
        self._pool = account_pool or AccountPool()

    async def fulfill_order(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: int,
        default_max_active_rentals: int,
        *,
        notify_unavailable: bool = True,
    ) -> Rental | None:
        # Serialize the live FunPay callback and scheduled/manual retries for
        # the same order. Together with uq_rental_order this prevents two
        # workers from allocating and delivering two independent rentals.
        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if order is None:
            raise KeyError(f"Order {order_id} not found")
        rental = await self._find_rental_by_order(session, order_id)
        if not await is_verified_bot_sale_order(session, order):
            if rental is not None:
                self._quarantine_unverified_rental(rental)
                await session.commit()
                return rental
            raise UnverifiedOrderProvenanceError(
                f"Order {order_id} has no exact verified bot-sale provenance"
            )
        if rental is not None:
            if not self._claim_existing_delivery(order, rental):
                return rental
            await session.commit()
            return await self._deliver_claimed(
                session, gateway, order.id, rental.id,
            )
        if order.status not in _FULFILLABLE_ORDER_STATUSES:
            return None

        duration = await session.get(Duration, order.duration_id)
        if duration is None:
            raise KeyError(f"Duration {order.duration_id} not found")

        scope = await session.get(LimitScope, order.limit_scope_id)
        scope_code = scope.code if scope else "any"

        criteria = AccountCriteria(
            tier_id=order.tier_id,
            duration_minutes=duration.minutes,
            scope=scope_code,
            min_limit_pct=order.min_limit_pct,
            max_5h_pct=order.max_5h_pct,
            max_weekly_pct=order.max_weekly_pct,
        )
        account = await self._pool.acquire(session, criteria, default_max_active_rentals)
        if account is None:
            if notify_unavailable:
                await self._send_no_account(session, gateway, order)
            return None

        limits = await session.get(AccountLimits, account.id)
        now = datetime.now(timezone.utc)
        rental = Rental(
            order_id=order.id,
            account_id=account.id,
            buyer_funpay_id=order.buyer_funpay_id,
            buyer_funpay_chat_id=order.funpay_chat_id,
            tier_id=order.tier_id,
            duration_id=order.duration_id,
            limit_scope_id=order.limit_scope_id,
            min_limit_pct=order.min_limit_pct,
            max_5h_pct=order.max_5h_pct,
            max_weekly_pct=order.max_weekly_pct,
            lang=order.buyer_locale or "ru",
            started_at=now,
            expires_at=now + timedelta(minutes=duration.minutes),
            status="active",
            issued_codex_5h_pct=limits.codex_5h_remaining_pct if limits else None,
            issued_codex_weekly_pct=limits.codex_weekly_remaining_pct if limits else None,
            issued_codex_primary_pct=(
                limits.codex_primary_remaining_pct if limits else None
            ),
            issued_codex_primary_window_seconds=(
                limits.codex_primary_window_seconds if limits else None
            ),
            issued_codex_primary_resets_at=(
                limits.codex_primary_resets_at if limits else None
            ),
            issued_codex_secondary_pct=(
                limits.codex_secondary_remaining_pct if limits else None
            ),
            issued_codex_secondary_window_seconds=(
                limits.codex_secondary_window_seconds if limits else None
            ),
            issued_codex_secondary_resets_at=(
                limits.codex_secondary_resets_at if limits else None
            ),
            issued_plan_window_status=(
                limits.plan_window_status if limits else None
            ),
            issued_expected_long_window_seconds=(
                limits.expected_long_window_seconds if limits else None
            ),
            issued_limits_measured_at=limits.measured_at if limits else None,
            credentials_delivery_status="sending",
            credentials_delivery_template="welcome",
            credentials_delivery_started_at=now,
            credentials_delivery_next_attempt_at=None,
            credentials_delivery_attempts=0,
        )
        session.add(rental)
        await session.flush()
        # Persist the allocation before external delivery. If the process dies
        # after FunPay accepts the message, retries can only reuse this Rental
        # and therefore never allocate a second account for the same order.
        await session.commit()
        return await self._deliver_claimed(session, gateway, order.id, rental.id)

    async def deliver_claimed_rental(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        rental_id: int,
    ) -> Rental:
        """Deliver a previously committed primary/replacement credential claim."""
        # Read only the FK here. ``_deliver_claimed`` establishes the global
        # Order -> Rental lock order shared with refund processing.
        order_id = await session.scalar(
            select(Rental.order_id).where(Rental.id == rental_id)
        )
        if order_id is None:
            raise KeyError(f"Rental {rental_id} not found")
        return await self._deliver_claimed(
            session, gateway, order_id, rental_id,
        )

    async def revoke_rental(self, session: AsyncSession, rental_id: int) -> Rental:
        rental = await session.get(Rental, rental_id)
        if rental is None:
            raise KeyError(f"Rental {rental_id} not found")
        rental.status = "revoked"
        await session.flush()
        return rental

    async def _find_rental_by_order(
        self, session: AsyncSession, order_id: int,
    ) -> Rental | None:
        result = await session.execute(
            select(Rental).where(Rental.order_id == order_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _claim_existing_delivery(order: Order, rental: Rental) -> bool:
        if (
            order.status not in _FULFILLABLE_ORDER_STATUSES
            or rental.status != "active"
            or rental.credentials_delivery_status == "sent"
            or rental.credentials_delivery_status == "manual"
            or rental.credentials_delivery_attempts
            >= CREDENTIAL_DELIVERY_MAX_ATTEMPTS
        ):
            return False

        now = datetime.now(timezone.utc)
        next_attempt_at = rental.credentials_delivery_next_attempt_at
        if next_attempt_at is not None and next_attempt_at.tzinfo is None:
            next_attempt_at = next_attempt_at.replace(tzinfo=timezone.utc)
        if next_attempt_at is not None and next_attempt_at > now:
            return False

        started_at = rental.credentials_delivery_started_at
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if (
            rental.credentials_delivery_status == "sending"
            and started_at is not None
            and started_at > now - CREDENTIAL_DELIVERY_LEASE
        ):
            return False

        rental.credentials_delivery_status = "sending"
        rental.credentials_delivery_started_at = now
        rental.credentials_delivery_next_attempt_at = None
        rental.credentials_delivery_last_error = None
        return True

    async def _deliver_claimed(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: int,
        rental_id: int,
    ) -> Rental:
        # Reacquire the order lock after the allocation commit. Refund handling
        # uses the same lock, so credentials cannot race a completed refund.
        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        rental = (
            await session.execute(
                select(Rental)
                .where(Rental.id == rental_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if rental is None:
            raise KeyError(f"Rental {rental_id} not found")
        if not await is_verified_bot_sale_order(session, order):
            self._quarantine_unverified_rental(rental)
            await session.commit()
            return rental
        if (
            order.status not in _FULFILLABLE_ORDER_STATUSES
            or rental.status != "active"
            or rental.credentials_delivery_status != "sending"
        ):
            if rental.credentials_delivery_status == "sending":
                rental.credentials_delivery_status = "failed"
                rental.credentials_delivery_last_error = "order_not_fulfillable"
                rental.credentials_delivery_next_attempt_at = None
                await session.commit()
            return rental

        account = (
            await session.execute(
                select(Account)
                .where(Account.id == rental.account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        duration = await session.get(Duration, rental.duration_id)
        if account is None or duration is None:
            rental.credentials_delivery_status = "manual"
            rental.credentials_delivery_last_error = "delivery_data_missing"
            rental.credentials_delivery_next_attempt_at = None
            self._record_manual_delivery_required(session, rental)
            await session.commit()
            return rental
        limits = await self._delivery_limits_if_eligible(
            session,
            account,
            rental,
            duration,
        )
        if (
            limits is None
            and rental.credentials_delivery_template == "welcome"
            # Once any external send was attempted, an exception/timeout is
            # ambiguous: FunPay may already have accepted the credentials.
            # Never switch that buyer to a second account on a retry.
            and rental.credentials_delivery_attempts == 0
        ):
            reallocated = await self._reallocate_initial_delivery(
                session, rental, duration, account.id,
            )
            if reallocated is not None:
                # Reallocation persists the target and releases all locks.
                # Refund handling uses the same Order lock, so reacquire and
                # repopulate every authorization row before disclosing data.
                order = (
                    await session.execute(
                        select(Order)
                        .where(Order.id == order_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                rental = (
                    await session.execute(
                        select(Rental)
                        .where(Rental.id == rental_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if not await is_verified_bot_sale_order(session, order):
                    self._quarantine_unverified_rental(rental)
                    await session.commit()
                    return rental
                if (
                    order.status not in _FULFILLABLE_ORDER_STATUSES
                    or rental.status != "active"
                    or rental.credentials_delivery_status != "sending"
                    or rental.account_id != reallocated.id
                ):
                    if rental.credentials_delivery_status == "sending":
                        rental.credentials_delivery_status = "failed"
                        rental.credentials_delivery_last_error = (
                            "order_not_fulfillable_after_reallocation"
                        )
                        rental.credentials_delivery_next_attempt_at = None
                        await session.commit()
                    return rental
                account = (
                    await session.execute(
                        select(Account)
                        .where(Account.id == reallocated.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                limits = await self._delivery_limits_if_eligible(
                    session, account, rental, duration,
                )
        if limits is None:
            rental.credentials_delivery_status = "manual"
            rental.credentials_delivery_last_error = (
                "delivery_account_no_longer_eligible"
            )
            rental.credentials_delivery_next_attempt_at = None
            self._record_manual_delivery_required(session, rental)
            await session.commit()
            return rental
        self._copy_issued_limit_snapshot(rental, limits)

        # Persist the earliest possible disclosure boundary before external
        # I/O. If the process dies after FunPay accepts the message, expiry can
        # still revoke this account at a finite deadline. A retry never changes
        # the account target and never grants a fresh full term.
        send_started_at = datetime.now(timezone.utc)
        attempt_number = rental.credentials_delivery_attempts + 1
        rental.credentials_delivery_attempts = attempt_number
        rental.credentials_delivery_started_at = send_started_at
        if (
            rental.credentials_delivery_template == "welcome"
            and attempt_number == 1
        ):
            rental.started_at = send_started_at
            rental.expires_at = send_started_at + timedelta(
                minutes=duration.minutes
            )
        expected_account_id = rental.account_id
        await session.commit()

        # Committing the disclosure boundary releases every lock. Reacquire in
        # the canonical Order -> Rental -> Account order and re-authorize once
        # more before sending any secret.
        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        rental = (
            await session.execute(
                select(Rental)
                .where(Rental.id == rental_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if not await is_verified_bot_sale_order(session, order):
            self._quarantine_unverified_rental(rental)
            await session.commit()
            return rental
        if (
            order.status not in _FULFILLABLE_ORDER_STATUSES
            or rental.status != "active"
            or rental.credentials_delivery_status != "sending"
            or rental.account_id != expected_account_id
            or rental.credentials_delivery_attempts != attempt_number
        ):
            if rental.credentials_delivery_status == "sending":
                rental.credentials_delivery_status = "failed"
                rental.credentials_delivery_last_error = (
                    "delivery_state_changed_before_send"
                )
                rental.credentials_delivery_next_attempt_at = None
                await session.commit()
            return rental
        account = (
            await session.execute(
                select(Account)
                .where(Account.id == expected_account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if account is None:
            limits = None
        else:
            limits = await self._delivery_limits_if_eligible(
                session, account, rental, duration,
            )
        if limits is None:
            rental.credentials_delivery_status = "manual"
            rental.credentials_delivery_last_error = (
                "delivery_account_no_longer_eligible_after_boundary"
            )
            rental.credentials_delivery_next_attempt_at = None
            self._record_manual_delivery_required(session, rental)
            await session.commit()
            return rental
        self._copy_issued_limit_snapshot(rental, limits)

        if rental.credentials_delivery_template == "welcome":
            access_duration_seconds = (
                None
                if attempt_number == 1
                else max(
                    1,
                    (_as_utc(rental.expires_at) - send_started_at)
                    .total_seconds(),
                )
            )
            access_duration_minutes = (
                duration.minutes
                if access_duration_seconds is None
                else max(1, math.ceil(access_duration_seconds / 60))
            )
            access_expires_at = None
        else:
            access_expires_at = rental.expires_at
            if access_expires_at.tzinfo is None:
                access_expires_at = access_expires_at.replace(
                    tzinfo=timezone.utc
                )
            access_duration_seconds = max(
                1,
                (access_expires_at - send_started_at).total_seconds(),
            )
            access_duration_minutes = max(
                1,
                math.ceil(access_duration_seconds / 60),
            )
        try:
            await self._send_welcome(
                session,
                gateway,
                order,
                account,
                rental,
                access_duration_minutes,
                access_duration_seconds,
                access_expires_at,
                template_key=rental.credentials_delivery_template,
            )
        except Exception as exc:
            self._schedule_delivery_retry(
                session,
                rental,
                f"delivery_failed:{type(exc).__name__}"[:128],
            )
            await session.commit()
            raise

        # Access starts only after FunPay confirmed delivery. The welcome
        # message intentionally advertises the exact duration rather than an
        # unknowable pre-send absolute deadline.
        delivered_at = datetime.now(timezone.utc)
        rental.credentials_delivery_status = "sent"
        rental.credentials_delivered_at = delivered_at
        rental.credentials_delivery_next_attempt_at = None
        rental.credentials_delivery_last_error = None
        # Only the initial paid rental begins at credential delivery. A
        # replacement must preserve the customer's original expiry instead of
        # silently granting a new full term.
        if (
            rental.credentials_delivery_template == "welcome"
            and attempt_number == 1
        ):
            access_expires_at = delivered_at + timedelta(
                minutes=access_duration_minutes
            )
            rental.started_at = delivered_at
            rental.expires_at = access_expires_at
        await session.commit()
        return rental

    @staticmethod
    def _quarantine_unverified_rental(rental: Rental) -> None:
        """Make every automatic delivery retry fail closed without deleting audit data."""

        if rental.credentials_delivery_status != "sent":
            rental.credentials_delivery_status = "manual"
        rental.credentials_delivery_last_error = "unverified_order_provenance"
        rental.credentials_delivery_next_attempt_at = None

    async def _delivery_limits_if_eligible(
        self,
        session: AsyncSession,
        account: Account,
        rental: Rental,
        duration: Duration,
    ) -> AccountLimits | None:
        """Re-authorize the reserved account immediately before disclosure.

        A retry may happen long after allocation. Never disclose credentials
        from an account whose plan, measured limits, status or subscription
        headroom changed while FunPay delivery was unavailable.
        """

        now = datetime.now(timezone.utc)
        tier = (
            await session.execute(
                select(SubscriptionTier)
                .where(SubscriptionTier.id == rental.tier_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        scope = (
            await session.execute(
                select(LimitScope)
                .where(LimitScope.id == rental.limit_scope_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            tier is None
            or scope is None
            or not tier.is_active
            or not tier.is_sellable
            or not scope.is_enabled
            or scope.code not in {"any", "codex"}
        ):
            return None

        if (
            rental.credentials_delivery_template == "welcome"
            and rental.credentials_delivery_attempts == 0
        ):
            required_until = (
                now
                + timedelta(minutes=duration.minutes)
                + INITIAL_DELIVERY_SUBSCRIPTION_HEADROOM
            )
            freshness_minutes = duration.minutes
        else:
            required_until = rental.expires_at
            if required_until.tzinfo is None:
                required_until = required_until.replace(tzinfo=timezone.utc)
            if required_until - now <= REPLACEMENT_DELIVERY_MIN_REMAINING:
                return None
            freshness_minutes = max(
                1,
                int((required_until - now).total_seconds() // 60),
            )

        freshness = limits_freshness_for_duration(freshness_minutes)
        expiry_condition = Account.subscription_expires_at >= required_until
        if tier.code == "free":
            expiry_condition = or_(
                expiry_condition,
                Account.subscription_expires_at.is_(None),
            )
        stmt = (
            select(AccountLimits)
            .select_from(Account)
            .join(AccountLimits, AccountLimits.account_id == Account.id)
            .join(SubscriptionTier, SubscriptionTier.id == Account.tier_id)
            .where(
                Account.id == account.id,
                Account.status == "active",
                Account.operator_status_override.is_(None),
                Account.tier_id == rental.tier_id,
                SubscriptionTier.is_active.is_(True),
                SubscriptionTier.is_sellable.is_(True),
                expiry_condition,
                AccountLimits.measured_at >= now - freshness,
                AccountLimits.refresh_status == "ok",
                AccountLimits.plan_window_status == "ok",
            )
        )
        stmt = apply_limit_scope_filters(
            stmt,
            scope=scope.code,
            min_limit_pct=rental.min_limit_pct,
            max_short_pct=rental.max_5h_pct,
            max_long_pct=rental.max_weekly_pct,
        )
        return (
            await session.execute(
                stmt.limit(1).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _reallocate_initial_delivery(
        self,
        session: AsyncSession,
        rental: Rental,
        duration: Duration,
        old_account_id: int,
    ) -> Account | None:
        """Move an unsent initial rental off an account that became unsafe."""

        scope = await session.get(LimitScope, rental.limit_scope_id)
        if scope is None or scope.code not in {"any", "codex"}:
            return None
        criteria = AccountCriteria(
            tier_id=rental.tier_id,
            duration_minutes=duration.minutes,
            scope=scope.code,
            min_limit_pct=rental.min_limit_pct,
            max_5h_pct=rental.max_5h_pct,
            max_weekly_pct=rental.max_weekly_pct,
        )
        account = await self._pool.acquire_excluding(
            session,
            criteria,
            exclude_account_id=old_account_id,
            default_max_active_rentals=1,
        )
        if account is None:
            return None
        rental.account_id = account.id
        session.add(
            AuditLog(
                event_type="credential_delivery_reallocated",
                account_id=account.id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={"old_account_id": old_account_id},
            )
        )
        # Persist the exact new target before any external message is sent.
        await session.commit()
        return account

    @staticmethod
    def _copy_issued_limit_snapshot(
        rental: Rental,
        limits: AccountLimits,
    ) -> None:
        """Make the durable buyer claim match the final eligibility sample."""

        rental.issued_codex_5h_pct = limits.codex_5h_remaining_pct
        rental.issued_codex_weekly_pct = limits.codex_weekly_remaining_pct
        rental.issued_codex_primary_pct = limits.codex_primary_remaining_pct
        rental.issued_codex_primary_window_seconds = (
            limits.codex_primary_window_seconds
        )
        rental.issued_codex_primary_resets_at = limits.codex_primary_resets_at
        rental.issued_codex_secondary_pct = limits.codex_secondary_remaining_pct
        rental.issued_codex_secondary_window_seconds = (
            limits.codex_secondary_window_seconds
        )
        rental.issued_codex_secondary_resets_at = (
            limits.codex_secondary_resets_at
        )
        rental.issued_plan_window_status = limits.plan_window_status
        rental.issued_expected_long_window_seconds = (
            limits.expected_long_window_seconds
        )
        rental.issued_limits_measured_at = limits.measured_at

    @staticmethod
    def _schedule_delivery_retry(
        session: AsyncSession,
        rental: Rental,
        error: str,
    ) -> None:
        rental.credentials_delivery_last_error = error[:128]
        if rental.credentials_delivery_attempts >= CREDENTIAL_DELIVERY_MAX_ATTEMPTS:
            rental.credentials_delivery_status = "manual"
            rental.credentials_delivery_next_attempt_at = None
            RentalService._record_manual_delivery_required(session, rental)
            return

        delay_seconds = credential_delivery_retry_delay_seconds(
            rental.credentials_delivery_attempts,
        )
        rental.credentials_delivery_status = "failed"
        rental.credentials_delivery_next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        )

    @staticmethod
    def _record_manual_delivery_required(
        session: AsyncSession,
        rental: Rental,
    ) -> None:
        session.add(
            AuditLog(
                event_type="credential_delivery_manual_required",
                account_id=rental.account_id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={
                    "attempts": rental.credentials_delivery_attempts,
                    "error": rental.credentials_delivery_last_error,
                },
            )
        )

    async def _send_welcome(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order: Order,
        account: Account,
        rental: Rental,
        duration_minutes: int,
        duration_seconds: float | None,
        access_expires_at: datetime | None,
        *,
        template_key: str = "welcome",
    ) -> None:
        tier = await session.get(SubscriptionTier, account.tier_id)
        # account.password_encrypted использует FernetEncrypted TypeDecorator:
        # ORM автоматически расшифровывает при чтении, отдавая plaintext.
        password = account.password_encrypted
        lang = order.buyer_locale or "ru"
        if template_key not in {"welcome", "replace_success"}:
            template_key = "welcome"
        duration_text = (
            format_remaining_seconds(duration_seconds, lang)
            if duration_seconds is not None
            else format_duration(duration_minutes, lang)
        )
        variables = dict(
            login=account.login,
            password=password,
            tier=tier.name if tier else "",
            duration=duration_text,
            duration_minutes=duration_minutes,
            days=format_legacy_days(duration_minutes),
            expires_at=format_plan_expiry(
                account.subscription_expires_at, lang
            ),
            **issued_usage_template_variables(rental, lang=lang),
        )
        if access_expires_at is not None:
            variables["access_expires_at"] = format_access_expiry(
                access_expires_at, lang
            )
        text = await render_message(
            session, template_key, lang, **variables,
        )
        await asyncio.wait_for(
            gateway.send_message(chat_id=int(order.funpay_chat_id), text=text),
            timeout=CREDENTIAL_SEND_TIMEOUT_SECONDS,
        )

    async def _send_no_account(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order: Order,
    ) -> None:
        lang = order.buyer_locale or "ru"
        retry_minutes = await self._retry_minutes(session)
        text = await render_message(
            session, "no_account_available", lang, retry_minutes=retry_minutes,
        )
        try:
            await asyncio.wait_for(
                gateway.send_message(
                    chat_id=int(order.funpay_chat_id), text=text,
                ),
                timeout=CREDENTIAL_SEND_TIMEOUT_SECONDS,
            )
        except Exception:
            # ``fulfill_order`` still owns the Order row lock on this path.
            # Release it promptly when FunPay is unavailable instead of
            # blocking refund and scheduled fulfillment until a hung socket
            # eventually fails on its own.
            await session.rollback()
            raise

    @staticmethod
    async def _retry_minutes(session: AsyncSession) -> int:
        settings = await session.get(SellerSettings, 1)
        if settings is not None:
            return settings.limits_check_interval_minutes
        return 5
