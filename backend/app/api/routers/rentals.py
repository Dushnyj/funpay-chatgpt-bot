from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
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
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(rental, field, value)
    await session.commit()
    await session.refresh(rental)
    return rental
