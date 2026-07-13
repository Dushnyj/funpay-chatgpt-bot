from __future__ import annotations

import math
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.integrations.funpay.types import OfferFieldsDTO
from app.models.lot import Lot


_PROVENANCE_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_PROVENANCE_MARKER_RE = re.compile(r"\[FPBOT:([0-9a-f]{32})\]")
_FUNPAY_DESCRIPTION_MAX_LENGTH = 4000


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
    clean = _PROVENANCE_MARKER_RE.sub("", description or "").strip()
    separator = "\n\n" if clean else ""
    available = _FUNPAY_DESCRIPTION_MAX_LENGTH - len(separator) - len(marker)
    clean = clean[: max(0, available)].rstrip()
    separator = "\n\n" if clean else ""
    return f"{clean}{separator}{marker}"


def extract_provenance_token(full_description: str | None) -> str | None:
    """Extract one exact canonical marker; duplicates fail closed."""

    matches = _PROVENANCE_MARKER_RE.findall(full_description or "")
    return matches[0] if len(matches) == 1 else None


def build_offer_fields(lot: Lot, offer_id: int, active: bool) -> OfferFieldsDTO:
    """Сборка OfferFieldsDTO из доменного Lot.

    offer_id=0 — создание нового лота на FunPay.
    subcategory_id = lot.funpay_node_id (ID ноды FunPay, куда публикуется лот).
    """
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
        price=float(lot.price),
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
        fields = build_offer_fields(lot, offer_id=offer_id, active=active)
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
        """Adopt one exact, locally-unclaimed offer before creating another.

        A process can die after FunPay accepts a create but before ``funpay_id``
        is committed. Matching the deterministic title and price makes the
        next reconciliation recover that offer instead of duplicating it.
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
        titles = {
            _normalize_title(lot.title_ru),
            _normalize_title(lot.title_en),
        }
        titles.discard("")
        matches = [
            offer for offer in remote
            if offer.offer_id not in used_ids
            and _normalize_title(offer.title) in titles
            and offer.price is not None
            and math.isclose(offer.price, float(lot.price), abs_tol=0.01)
        ]
        if len(matches) == 1:
            return matches[0].offer_id
        if len(matches) > 1:
            raise RuntimeError(
                "More than one unclaimed FunPay offer matches the local lot"
            )
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
