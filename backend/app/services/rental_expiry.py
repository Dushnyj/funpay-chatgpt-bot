from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.catalog import Duration, SubscriptionTier
from app.models.rental import Rental
from app.services.messages import render_message


class RentalExpiryService:
    """Поиск истёкших аренд, пометка expired, отправка expiry message."""

    async def expire_overdue(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
    ) -> list[Rental]:
        """Помечает все active аренды с expires_at <= NOW() как expired.

        Отправляет expiry_message в чат каждой истёкшей аренды.
        Возвращает список обработанных Rental.
        """
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(Rental).where(
                Rental.status == "active",
                Rental.expires_at <= now,
            )
        )
        overdue = result.scalars().all()
        expired_list: list[Rental] = []

        for rental in overdue:
            rental.status = "expired"
            await self._send_expiry(session, gateway, rental)
            expired_list.append(rental)

        if expired_list:
            await session.flush()
        return expired_list

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
