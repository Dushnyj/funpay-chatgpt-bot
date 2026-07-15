from __future__ import annotations

import math
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.integrations.funpay.provenance import (
    PROVENANCE_MARKER_RE,
    descriptions_have_exact_provenance,
    exact_provenance_token,
)
from app.integrations.funpay.types import OfferFieldsDTO
from app.models.catalog import SubscriptionTier
from app.models.lot import Lot
from app.services.funpay_offer_mapping import funpay_offer_plan_fields


_PROVENANCE_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_FUNPAY_DESCRIPTION_MAX_LENGTH = 4000
_PAYMENT_MESSAGE_RU = (
    "Заказ принят. Бот отправит данные для входа в чат заказа. "
    "Код для входа: !код. Помощь: !помощь."
)
_PAYMENT_MESSAGE_EN = (
    "Order accepted. The bot will send sign-in details in the order chat. "
    "Sign-in code: !code. Help: !help."
)


def provenance_marker(token: str) -> str:
    """Return the canonical marker for one persisted lot token."""

    if not _PROVENANCE_TOKEN_RE.fullmatch(token):
        raise ValueError("Lot provenance token must be 32 lowercase hex characters")
    return f"[FPBOT:{token}]"


def description_with_provenance_marker(
    description: str | None,
    token: str,
) -> str:
    """Append exactly one stable marker without exceeding FunPay's limit."""

    marker = provenance_marker(token)
    # Operator text may have been copied back from FunPay. Remove every old
    # canonical marker before appending the authoritative current one.
    clean = PROVENANCE_MARKER_RE.sub("", description or "").strip()
    separator = "\n\n" if clean else ""
    available = _FUNPAY_DESCRIPTION_MAX_LENGTH - len(separator) - len(marker)
    clean = clean[: max(0, available)].rstrip()
    separator = "\n\n" if clean else ""
    return f"{clean}{separator}{marker}"


def extract_provenance_token(full_description: str | None) -> str | None:
    """Extract one exact canonical marker; duplicates fail closed."""

    return exact_provenance_token((full_description,))


def build_offer_fields(
    lot: Lot,
    tier: SubscriptionTier,
    offer_id: int,
    active: bool,
) -> OfferFieldsDTO:
    """Сборка OfferFieldsDTO из доменного Lot.

    offer_id=0 — создание нового лота на FunPay.
    subcategory_id = lot.funpay_node_id (ID ноды FunPay, куда публикуется лот).
    """
    plan_fields = funpay_offer_plan_fields(tier)
    return OfferFieldsDTO(
        offer_id=offer_id,
        subcategory_id=lot.funpay_node_id or 0,
        title_ru=lot.title_ru,
        title_en=lot.title_en,
        desc_ru=description_with_provenance_marker(
            lot.description_ru,
            lot.provenance_token,
        ),
        desc_en=description_with_provenance_marker(
            lot.description_en,
            lot.provenance_token,
        ),
        payment_msg_ru=_PAYMENT_MESSAGE_RU,
        payment_msg_en=_PAYMENT_MESSAGE_EN,
        subscription=plan_fields.subscription,
        subscription_type=plan_fields.subscription_type,
        price=float(lot.price),
        # The reconciler advertises only one immediately allocatable unit and
        # re-runs after every durable allocation/capacity change.  This keeps
        # FunPay stock aligned with the bot's single-rental-per-account rule.
        amount=1,
        active=active,
        auto_delivery=False,
    )


class LotNotPublishedError(Exception):
    """Лот ещё не опубликован на FunPay (funpay_id is None)."""


class LotNotFoundError(Exception):
    """Lot с указанным ID не найден в БД."""


class LotSyncService:
    """Синхронизация состояния лота между БД и FunPay.

    sync_lot: создаёт новый (funpay_id is None) или обновляет существующий.
    pause_lot/activate_lot: переключение active без перезаписи остальных полей.
    """

    async def sync_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot_id: int,
        active: bool,
    ) -> int:
        lot = await self._get_lot(session, lot_id)
        if lot.funpay_id:
            offer_id = int(lot.funpay_id)
        else:
            offer_id = await self._recover_uncommitted_remote_offer(
                session, gateway, lot,
            )
        tier = await session.get(SubscriptionTier, lot.tier_id)
        if tier is None:
            raise RuntimeError(f"Subscription tier {lot.tier_id} not found")
        fields = build_offer_fields(
            lot,
            tier,
            offer_id=offer_id,
            active=active,
        )
        result_id = await gateway.save_offer_fields(fields)
        if result_id <= 0:
            raise RuntimeError("FunPay did not return a valid offer id")
        if not lot.funpay_id:
            lot.funpay_id = str(result_id)
        lot.provenance_marker_synced = True
        await session.flush()
        return result_id

    async def _recover_uncommitted_remote_offer(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot: Lot,
    ) -> int:
        """Recover only an offer carrying this lot's immutable bot marker.

        A title and price are public, operator-editable display attributes and
        cannot prove that the bot created an offer.  They only bound the small
        set whose full descriptions are fetched; adoption requires the exact
        persisted provenance token.  A manual lookalike is therefore left
        untouched and normal creation proceeds.
        """
        remote = await gateway.get_my_offers(lot.funpay_node_id or 0)
        used_result = await session.execute(
            select(Lot.funpay_id).where(
                Lot.id != lot.id,
                Lot.funpay_id.isnot(None),
            )
        )
        used_ids = {
            int(value) for value in used_result.scalars()
            if value is not None and str(value).isdigit()
        }
        candidates = [
            offer for offer in remote
            if offer.offer_id not in used_ids
            and _offer_preview_matches_lot(offer, lot)
        ]
        # FunPay appends form attributes to preview titles (for example
        # ``, Без подписки``). Prefix+price bounds full-form reads so a
        # recovery does not inspect the seller's whole catalog. The exact
        # immutable marker below remains the sole adoption authority, so
        # manual lookalikes are never adopted.
        matches = []
        for offer in candidates:
            try:
                desc_ru, desc_en = await gateway.get_offer_descriptions(
                    offer.offer_id
                )
            except Exception as exc:
                raise RuntimeError(
                    "Unable to verify provenance of an unclaimed FunPay offer"
                ) from exc
            if descriptions_have_exact_provenance(
                (desc_ru, desc_en), lot.provenance_token,
            ):
                matches.append(offer)
        if matches:
            # A process from an older release could retry after a successful
            # remote save and create more than one offer with the same token.
            # Every match is bot-owned; keep the oldest deterministic ID and
            # fail closed unless all duplicates are made unavailable.
            matches.sort(key=lambda offer: offer.offer_id)
            canonical, *duplicates = matches
            for duplicate in duplicates:
                changed = await gateway.set_offer_active(
                    duplicate.offer_id,
                    active=False,
                )
                if not changed:
                    raise RuntimeError(
                        "Unable to pause a duplicate FunPay offer carrying "
                        "the local lot marker"
                    )
            return canonical.offer_id
        return 0

    async def pause_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot_id: int,
    ) -> None:
        lot = await self._get_lot(session, lot_id)
        if not lot.funpay_id:
            raise LotNotPublishedError(f"Lot {lot_id} has no funpay_id")
        changed = await gateway.set_offer_active(int(lot.funpay_id), active=False)
        if not changed:
            raise RuntimeError(f"FunPay did not pause offer {lot.funpay_id}")
        lot.status = "paused"
        await session.flush()

    async def activate_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot_id: int,
    ) -> None:
        lot = await self._get_lot(session, lot_id)
        if not lot.funpay_id:
            raise LotNotPublishedError(f"Lot {lot_id} has no funpay_id")
        if not lot.provenance_marker_synced:
            # A plain activation would leave a legacy offer without the
            # mandatory ownership marker. Full sync publishes the marker and
            # activates the same remote offer atomically from our perspective.
            await self.sync_lot(session, gateway, lot_id, active=True)
            lot.status = "active"
            await session.flush()
            return
        changed = await gateway.set_offer_active(int(lot.funpay_id), active=True)
        if not changed:
            raise RuntimeError(f"FunPay did not activate offer {lot.funpay_id}")
        lot.status = "active"
        await session.flush()

    async def _get_lot(self, session: AsyncSession, lot_id: int) -> Lot:
        lot = await session.get(Lot, lot_id)
        if lot is None:
            raise LotNotFoundError(f"Lot {lot_id} not found")
        return lot


def _normalize_title(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _offer_preview_matches_lot(offer, lot: Lot) -> bool:
    preview_title = _normalize_title(offer.title)
    requested_titles = {
        _normalize_title(lot.title_ru),
        _normalize_title(lot.title_en),
    }
    requested_titles.discard("")
    title_matches = any(
        preview_title.startswith(title)
        for title in requested_titles
    )
    price_matches = (
        offer.price is not None
        and math.isclose(offer.price, float(lot.price), abs_tol=0.01)
    )
    return title_matches and price_matches
