from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import (
    TierCreate, TierOut, TierUpdate,
    DurationOut, DurationUpdate, LimitScopeOut,
)
from app.models.catalog import Duration, LimitScope, SubscriptionTier

router = APIRouter(prefix="/api", tags=["catalog"], dependencies=[Depends(get_current_user)])


@router.get("/tiers", response_model=list[TierOut])
async def list_tiers(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(SubscriptionTier).order_by(SubscriptionTier.id))
    return result.scalars().all()


@router.post("/tiers", response_model=TierOut, status_code=201)
async def create_tier(req: TierCreate, session: AsyncSession = Depends(get_db_session)):
    tier = SubscriptionTier(name=req.name, description=req.description, is_active=req.is_active)
    session.add(tier)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Tier name already exists")
    await session.refresh(tier)
    return tier


@router.patch("/tiers/{tier_id}", response_model=TierOut)
async def update_tier(tier_id: int, req: TierUpdate, session: AsyncSession = Depends(get_db_session)):
    tier = await session.get(SubscriptionTier, tier_id)
    if tier is None:
        raise HTTPException(status_code=404, detail="Tier not found")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(tier, field, value)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Tier name already exists")
    await session.refresh(tier)
    return tier


@router.delete("/tiers/{tier_id}", status_code=204)
async def delete_tier(tier_id: int, session: AsyncSession = Depends(get_db_session)):
    tier = await session.get(SubscriptionTier, tier_id)
    if tier is None:
        raise HTTPException(status_code=404, detail="Tier not found")
    await session.delete(tier)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Tier is used by accounts or lots")


@router.get("/durations", response_model=list[DurationOut])
async def list_durations(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Duration).order_by(Duration.sort_order))
    return result.scalars().all()


@router.get("/limit-scopes", response_model=list[LimitScopeOut])
async def list_limit_scopes(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(LimitScope).order_by(LimitScope.id))
    return result.scalars().all()
