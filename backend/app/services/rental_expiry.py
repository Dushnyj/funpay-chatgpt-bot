from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import CheckJobQueue
from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account
from app.models.audit import AuditLog
from app.models.catalog import Duration, SubscriptionTier
from app.models.rental import Order, Rental
from app.services.kick_service import KickService
from app.services.messages import render_message


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
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(Rental)
            .join(Order, Order.id == Rental.order_id)
            .where(
                Rental.status.in_(["active", "expiry_pending"]),
                Rental.expires_at <= now,
                Order.status.notin_(["refund_pending", "refunded"]),
            )
            .order_by(Rental.id)
            .with_for_update(skip_locked=True)
        )
        overdue = result.scalars().all()
        newly_overdue = [rental for rental in overdue if rental.status == "active"]

        for rental in overdue:
            rental.status = "expiry_pending"

        if overdue:
            await session.flush()

        # Revoke once per account, even when several rentals expire in the same
        # scheduler tick.  The status transition is deliberately retained when
        # the external kick fails: an expired buyer must never regain access.
        account_ids = {rental.account_id for rental in overdue}
        revoked: dict[int, bool] = {}
        for account_id in account_ids:
            revoked[account_id] = await self._revoke_account(
                session, gateway, account_id,
            )
        for rental in overdue:
            if (
                rental.status == "expiry_pending"
                and revoked[rental.account_id]
            ):
                rental.status = "expired"

        if gateway is not None:
            for rental in newly_overdue:
                try:
                    await self._send_expiry(session, gateway, rental)
                except Exception as exc:
                    session.add(AuditLog(
                        event_type="expiry_message_failed",
                        account_id=rental.account_id,
                        rental_id=rental.id,
                        chat_id=rental.buyer_funpay_chat_id,
                        metadata_={"error": str(exc)},
                    ))
        if overdue:
            await session.flush()
        return list(overdue)

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
                seconds = max(
                    0,
                    int((expires_at - datetime.now(timezone.utc)).total_seconds()),
                )
                hours = max(1, seconds // 3600) if seconds else 0
                expires_in = f"{hours // 24}д" if hours >= 24 else f"{hours}ч"
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

    async def _send_expiry(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        rental: Rental,
    ) -> None:
        tier = await session.get(SubscriptionTier, rental.tier_id)
        duration = await session.get(Duration, rental.duration_id)
        text = await render_message(
            session, "expiry", rental.lang,
            tier=tier.name if tier else "",
            days=duration.days if duration else 0,
        )
        await gateway.send_message(
            chat_id=int(rental.buyer_funpay_chat_id), text=text,
        )
