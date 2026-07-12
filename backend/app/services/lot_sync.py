from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.integrations.funpay.types import OfferFieldsDTO
from app.models.lot import Lot


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
        desc_ru=lot.description_ru or "",
        desc_en=lot.description_en or "",
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
            offer_id = 0
        fields = build_offer_fields(lot, offer_id=offer_id, active=active)
        result_id = await gateway.save_offer_fields(fields)
        if result_id <= 0:
            raise RuntimeError("FunPay did not return a valid offer id")
        if not lot.funpay_id:
            lot.funpay_id = str(result_id)
            await session.flush()
        return result_id

    async def pause_lot(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        lot_id: int,
    ) -> None:
        lot = await self._get_lot(session, lot_id)
        if not lot.funpay_id:
            raise LotNotPublishedError(f"Lot {lot_id} has no funpay_id")
        await gateway.set_offer_active(int(lot.funpay_id), active=False)
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
        await gateway.set_offer_active(int(lot.funpay_id), active=True)
        lot.status = "active"
        await session.flush()

    async def _get_lot(self, session: AsyncSession, lot_id: int) -> Lot:
        lot = await session.get(Lot, lot_id)
        if lot is None:
            raise LotNotFoundError(f"Lot {lot_id} not found")
        return lot
