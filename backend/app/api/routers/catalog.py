from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import (
    DurationOut,
    DurationPatch,
    DurationUpdate,
    LimitScopeOut,
    LimitScopeUpdate,
    TierCreate,
    TierOut,
    TierUpdate,
)
from app.models.catalog import Duration, LimitScope, SubscriptionTier

router = APIRouter(prefix="/api", tags=["catalog"], dependencies=[Depends(get_current_user)])


@router.get("/tiers", response_model=list[TierOut])
async def list_tiers(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(
        select(SubscriptionTier).order_by(SubscriptionTier.sort_order, SubscriptionTier.id)
    )
    return result.scalars().all()


@router.post("/tiers", response_model=TierOut, status_code=201)
async def create_tier(req: TierCreate, session: AsyncSession = Depends(get_db_session)):
    raise HTTPException(
        status_code=405,
        detail="Subscription tiers are synchronized by the application",
    )


@router.patch("/tiers/{tier_id}", response_model=TierOut)
async def update_tier(
    tier_id: int,
    req: TierUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    tier = await session.get(SubscriptionTier, tier_id)
    if tier is None:
        raise HTTPException(status_code=404, detail="Tier not found")
    changes = req.model_dump(exclude_unset=True)
    if changes.get("is_sellable") is True and not changes.get(
        "is_active", tier.is_active
    ):
        raise HTTPException(
            status_code=422,
            detail="An inactive tier cannot be enabled for sale",
        )
    if changes.get("is_active") is False:
        changes["is_sellable"] = False
    for field, value in changes.items():
        setattr(tier, field, value)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Tier name already exists")
    await session.refresh(tier)
    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is not None:
        try:
            await lifecycle.reconcile_lots()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="Tier saved, but FunPay reconciliation failed",
            ) from exc
    return tier


@router.delete("/tiers/{tier_id}", status_code=204)
async def delete_tier(tier_id: int, session: AsyncSession = Depends(get_db_session)):
    tier = await session.get(SubscriptionTier, tier_id)
    if tier is None:
        raise HTTPException(status_code=404, detail="Tier not found")
    raise HTTPException(
        status_code=405,
        detail="System subscription tiers cannot be deleted",
    )


@router.get("/durations", response_model=list[DurationOut])
async def list_durations(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(
        select(Duration).order_by(Duration.sort_order, Duration.days, Duration.id)
    )
    return result.scalars().all()


@router.patch("/durations/batch", response_model=list[DurationOut])
async def update_durations_batch(
    items: list[DurationUpdate],
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    if not items:
        raise HTTPException(status_code=422, detail="At least one duration is required")
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="Duplicate duration id")
    result = await session.execute(select(Duration).where(Duration.id.in_(ids)))
    durations = {duration.id: duration for duration in result.scalars().all()}
    missing = sorted(set(ids) - durations.keys())
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Durations not found: {', '.join(map(str, missing))}",
        )
    availability_changed = False
    for item in items:
        duration = durations[item.id]
        changes = item.model_dump(
            exclude_unset=True, exclude={"id"}
        )
        if (
            "is_enabled" in changes
            and duration.is_enabled != changes["is_enabled"]
        ):
            availability_changed = True
        for field, value in changes.items():
            setattr(duration, field, value)
    await session.commit()
    if availability_changed:
        await _reconcile_lots(request, "Durations saved")
    result = await session.execute(
        select(Duration).order_by(Duration.sort_order, Duration.days, Duration.id)
    )
    return result.scalars().all()


@router.patch("/durations/{duration_id}", response_model=DurationOut)
async def update_duration(
    duration_id: int,
    req: DurationPatch,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    duration = await session.get(Duration, duration_id)
    if duration is None:
        raise HTTPException(status_code=404, detail="Duration not found")
    changes = req.model_dump(exclude_unset=True)
    availability_changed = (
        "is_enabled" in changes
        and duration.is_enabled != changes["is_enabled"]
    )
    for field, value in changes.items():
        setattr(duration, field, value)
    await session.commit()
    await session.refresh(duration)
    if availability_changed:
        await _reconcile_lots(request, "Duration saved")
    return duration


@router.get("/limit-scopes", response_model=list[LimitScopeOut])
async def list_limit_scopes(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(
        select(LimitScope).order_by(LimitScope.sort_order, LimitScope.id)
    )
    return result.scalars().all()


@router.patch("/limit-scopes/{scope_id}", response_model=LimitScopeOut)
async def update_limit_scope(
    scope_id: int,
    req: LimitScopeUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    scope = await session.get(LimitScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Limit scope not found")
    changes = req.model_dump(exclude_unset=True)
    if (
        changes.get("is_enabled") is True
        and scope.code not in {"any", "codex"}
    ):
        raise HTTPException(
            status_code=422,
            detail="Only any and codex limit scopes can be enabled",
        )
    availability_changed = (
        "is_enabled" in changes
        and scope.is_enabled != changes["is_enabled"]
    )
    for field, value in changes.items():
        setattr(scope, field, value)
    await session.commit()
    await session.refresh(scope)
    if availability_changed:
        await _reconcile_lots(request, "Limit scope saved")
    return scope


async def _reconcile_lots(request: Request, saved_message: str) -> None:
    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is None:
        return
    try:
        await lifecycle.reconcile_lots()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{saved_message}, but FunPay reconciliation failed",
        ) from exc
