from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import CheckJobQueue
from app.integrations.funpay.gateway import ChatGateway
from app.integrations.funpay.types import OrderInfo
from app.models.account import Account
from app.models.audit import AuditLog
from app.models.lot import Lot
from app.models.rental import Order, Rental
from app.services.kick_service import KickResult, KickService


logger = logging.getLogger(__name__)

_REFUND_REVOKE_LEASE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class _RefundRevokeClaim:
    order_id: int
    rental_id: int
    account_id: int
    started_at: datetime


class LotNotFoundError(Exception):
    """Для заказа не найден Lot с matching funpay_node_id."""


class OrderProcessor:
    """Обработка событий заказа: создание, обновление статуса.

    Создание идемпотентно по funpay_order_id. Определяет lot по funpay_node_id.
    НЕ выдаёт аккаунт — это ответственность Фазы 4 (AccountPool).
    """

    def __init__(
        self,
        kick_service: KickService | None = None,
        job_queue: CheckJobQueue | None = None,
    ) -> None:
        self._kick = kick_service or KickService()
        self._jobs = job_queue or CheckJobQueue()

    async def process_new_sale(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: str,
    ) -> Order:
        existing = await self._find_order(session, order_id)
        if existing is not None:
            return existing

        info = await gateway.get_order(order_id)
        lot = await self._find_lot(session, info)
        if lot is None:
            raise LotNotFoundError(
                f"No unambiguous active Lot for funpay_node_id={info.subcategory_id} "
                f"(order {order_id})"
            )
        order = Order(
            funpay_order_id=info.order_id,
            funpay_chat_id=str(info.chat_id),
            buyer_funpay_id=str(info.buyer_id),
            buyer_locale=_infer_buyer_locale(info.title, lot),
            lot_id=lot.id,
            tier_id=lot.tier_id,
            duration_id=lot.duration_id,
            limit_scope_id=lot.limit_scope_id,
            min_limit_pct=lot.min_limit_pct,
            max_5h_pct=lot.max_5h_pct,
            max_weekly_pct=lot.max_weekly_pct,
            price=lot.price,
            status="pending",
        )
        session.add(order)
        await session.flush()
        return order

    async def process_sale_closed(
        self,
        session: AsyncSession,
        order_id: str,
    ) -> Order:
        order = await self._get_order_or_raise(session, order_id, for_update=True)
        # Refund and revoke states are terminal/monotonic. FunPay callbacks may
        # be duplicated or reordered, so a delayed close must never resurrect
        # a refunded order or cancel a pending credential revocation.
        if order.status in {"refunded", "refund_pending"}:
            return order
        order.status = "completed"
        await session.flush()
        return order

    async def process_sale_refunded(
        self,
        session: AsyncSession,
        order_id: str,
    ) -> Order:
        order, claim = await self._claim_refund_revoke(session, order_id)
        if claim is None:
            return order

        # The durable claim and maintenance state were committed above.  No
        # database row lock is held while browser/email network I/O runs.
        try:
            kick = await self._kick.kick(session, claim.account_id)
        except Exception as exc:
            await session.rollback()
            kick = KickResult(success=False, error=str(exc))
        else:
            # KickService may read or refresh mailbox credentials through this
            # session. Close that transaction before taking final row locks.
            try:
                await session.commit()
            except Exception:
                await session.rollback()

        return await self._finalize_refund_revoke(
            session,
            order_id=order_id,
            claim=claim,
            kick=kick,
        )

    async def _claim_refund_revoke(
        self,
        session: AsyncSession,
        order_id: str,
    ) -> tuple[Order, _RefundRevokeClaim | None]:
        """Short Order -> Rental -> Account claim committed before kick I/O."""

        order = await self._get_order_or_raise(
            session, order_id, for_update=True,
        )
        if order.status == "refunded":
            await session.commit()
            return order, None

        rental = (
            await session.execute(
                select(Rental)
                .where(Rental.order_id == order.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if rental is None:
            order.status = "refunded"
            await session.commit()
            return order, None
        if rental.status not in {"active", "expiry_pending"}:
            # A payment provider may report the refund after normal expiry or
            # failed-delivery revocation. The session is already closed, so no
            # second browser kick is needed; keep order/rental history
            # monotonic and consistent for the admin panel.
            previous_status = rental.status
            order.status = "refunded"
            rental.status = "refunded"
            rental.replacement_target_account_id = None
            rental.expiry_revoke_started_at = None
            session.add(AuditLog(
                event_type="late_refund_terminal_rental",
                account_id=rental.account_id,
                order_id=order.id,
                rental_id=rental.id,
                metadata_={"previous_status": previous_status},
            ))
            await session.commit()
            return order, None

        account = (
            await session.execute(
                select(Account)
                .where(Account.id == rental.account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        order.status = "refund_pending"
        if account is not None:
            account.status = "maintenance"

        now = datetime.now(timezone.utc)
        existing_claim = rental.expiry_revoke_started_at
        if existing_claim is not None and existing_claim.tzinfo is None:
            existing_claim = existing_claim.replace(tzinfo=timezone.utc)
        if (
            existing_claim is not None
            and existing_claim > now - _REFUND_REVOKE_LEASE
        ):
            # Another refund/expiry worker owns the account-wide logout.  The
            # pending state is durable and the scheduler will retry after that
            # owner finalizes or its lease becomes stale.
            await session.commit()
            return order, None

        # A crashed replacement may have left a stale promised target. The
        # refund worker already owns Order -> Rental here, so it can safely
        # release that old reservation before taking over the common lease.
        rental.replacement_target_account_id = None
        rental.expiry_revoke_started_at = now
        claim = _RefundRevokeClaim(
            order_id=order.id,
            rental_id=rental.id,
            account_id=rental.account_id,
            started_at=now,
        )
        await session.commit()
        return order, claim

    async def _finalize_refund_revoke(
        self,
        session: AsyncSession,
        *,
        order_id: str,
        claim: _RefundRevokeClaim,
        kick: KickResult,
    ) -> Order:
        """Finalize only the exact durable claim after reacquiring row locks."""

        order = await self._get_order_or_raise(
            session, order_id, for_update=True,
        )
        rental = (
            await session.execute(
                select(Rental)
                .where(Rental.id == claim.rental_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        stored_claim = rental.expiry_revoke_started_at if rental else None
        if stored_claim is not None and stored_claim.tzinfo is None:
            stored_claim = stored_claim.replace(tzinfo=timezone.utc)
        claim_started_at = claim.started_at
        if claim_started_at.tzinfo is None:
            claim_started_at = claim_started_at.replace(tzinfo=timezone.utc)
        owns_claim = stored_claim == claim_started_at
        state_matches = bool(
            owns_claim
            and rental is not None
            and rental.order_id == claim.order_id == order.id
            and rental.account_id == claim.account_id
            and rental.status in {"active", "expiry_pending"}
            and order.status == "refund_pending"
        )

        session.add(AuditLog(
            event_type="refund_account_kick",
            account_id=claim.account_id,
            order_id=order.id,
            rental_id=rental.id if rental is not None else None,
            chat_id=(rental.buyer_funpay_chat_id if rental is not None else None),
            metadata_={
                "success": kick.success,
                "deduplicated": kick.deduplicated,
                "error": kick.error,
                "claim_owned": owns_claim,
                "state_matched": state_matches,
            },
        ))
        if owns_claim and rental is not None:
            rental.expiry_revoke_started_at = None

        if kick.success and state_matches:
            rental.status = "refunded"
            order.status = "refunded"
            await self._jobs.enqueue(
                session,
                account_id=claim.account_id,
                priority="refresh_recover",
                job_type="refresh_recover",
            )
        elif not kick.success:
            logger.warning(
                "Refund %s remains pending: account %s revoke failed: %s",
                order_id,
                claim.account_id,
                kick.error,
            )
        elif not state_matches:
            logger.warning(
                "Refund %s revoke completed after its claim/state changed; "
                "leaving the current state untouched",
                order_id,
            )
        await session.commit()
        return order

    async def _find_order(self, session: AsyncSession, order_id: str) -> Order | None:
        result = await session.execute(
            select(Order).where(Order.funpay_order_id == order_id)
        )
        return result.scalar_one_or_none()

    async def _get_order_or_raise(
        self,
        session: AsyncSession,
        order_id: str,
        *,
        for_update: bool = False,
    ) -> Order:
        if for_update:
            result = await session.execute(
                select(Order)
                .where(Order.funpay_order_id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            order = result.scalar_one_or_none()
        else:
            order = await self._find_order(session, order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found")
        return order

    async def _find_lot(
        self,
        session: AsyncSession,
        info: OrderInfo,
    ) -> Lot | None:
        """Map a remote order to exactly one local lot.

        A remote offer id is authoritative.  FunPayBotEngine 0.7 does not
        expose it on a stock OrderPage, so the fallback progressively narrows
        candidates by subcategory, exact normalized title and exact price.
        Ambiguity is rejected: issuing credentials for the wrong duration or
        threshold is worse than leaving the order for manual intervention.
        """
        if info.offer_id is not None:
            result = await session.execute(
                select(Lot).where(
                    Lot.funpay_id == str(info.offer_id),
                    Lot.status == "active",
                )
            )
            exact = result.scalars().all()
            return exact[0] if len(exact) == 1 else None

        result = await session.execute(
            select(Lot).where(
                Lot.funpay_node_id == info.subcategory_id,
                Lot.status == "active",
            )
        )
        candidates = list(result.scalars().all())
        if not candidates:
            return None

        title = _normalize_title(info.title)
        if title:
            candidates = [
                lot for lot in candidates
                if title in {_normalize_title(lot.title_ru), _normalize_title(lot.title_en)}
            ]
        else:
            # The stock parser normally lacks a stable offer id. Without a
            # title there is no safe way to bind a purchase to a local lot.
            return None

        if info.price is not None:
            candidates = [
                lot for lot in candidates
                if math.isclose(float(lot.price), float(info.price), abs_tol=0.01)
            ]
        else:
            return None
        return candidates[0] if len(candidates) == 1 else None


def _normalize_title(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _infer_buyer_locale(remote_title: str | None, lot: Lot) -> str:
    """Use the localized offer title FunPay returned for the paid order."""

    title = _normalize_title(remote_title)
    ru = _normalize_title(lot.title_ru)
    en = _normalize_title(lot.title_en)
    if title and en and en != ru and title == en:
        return "en"
    return "ru"
