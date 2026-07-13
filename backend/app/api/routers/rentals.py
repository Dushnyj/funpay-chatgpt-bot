from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.app_lifecycle import FunPayUnavailableError
from app.api.schemas import RentalOut, RentalPatch
from app.models.rental import Rental

router = APIRouter(prefix="/api/rentals", tags=["rentals"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[RentalOut])
async def list_rentals(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Rental).order_by(Rental.id.desc()))
    return result.scalars().all()


@router.patch("/{rental_id}", response_model=RentalOut)
async def update_rental(rental_id: int, req: RentalPatch, session: AsyncSession = Depends(get_db_session)):
    rental = await session.get(Rental, rental_id)
    if rental is None:
        raise HTTPException(status_code=404, detail="Rental not found")
    if req.model_fields_set:
        raise HTTPException(
            status_code=409,
            detail=(
                "Rental status is managed by the paid-order, refund, and expiry "
                "workflows so credentials are revoked before a terminal state."
            ),
        )
    return rental


@router.post("/{rental_id}/retry-delivery", response_model=RentalOut)
async def retry_rental_delivery(
    rental_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is None or not hasattr(lifecycle, "retry_rental_delivery"):
        raise HTTPException(status_code=503, detail="FunPay runtime is unavailable")
    try:
        await lifecycle.retry_rental_delivery(rental_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Rental not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FunPayUnavailableError as exc:
        raise HTTPException(status_code=503, detail="FunPay is not connected") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="FunPay did not confirm credential delivery",
        ) from exc

    rental = await session.get(Rental, rental_id)
    if rental is None:
        raise HTTPException(status_code=404, detail="Rental not found")
    await session.refresh(rental)
    return rental
