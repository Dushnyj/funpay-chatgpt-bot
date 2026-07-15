from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import (
    LotTemplateCreate,
    LotTemplateOut,
    LotTemplateUpdate,
    TemplateOut,
    TemplateUpdate,
)
from app.models.catalog import LimitScope, SubscriptionTier
from app.models.lot import LotTemplate
from app.models.message import MessageTemplate
from app.services.lot_templates import (
    DEFAULT_LOT_TEMPLATES,
    LOT_TEMPLATE_FIELDS,
    LotTemplateValidationError,
    validate_lot_template_key,
    validate_lot_template_values,
)
from app.services.messages import (
    TemplateValidationError,
    allowed_template_fields,
    validate_template_content,
)
from app.services.seed_data import DEFAULT_MESSAGE_TEMPLATES

router = APIRouter(
    prefix="/api/templates",
    tags=["templates"],
    dependencies=[Depends(get_current_user)],
)


class TemplateUpdateResponse(BaseModel):
    updated: int


@router.get("", response_model=list[TemplateOut])
async def list_templates(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(
        select(MessageTemplate).order_by(MessageTemplate.key, MessageTemplate.lang)
    )
    return [_message_template_out(template) for template in result.scalars().all()]


@router.put("", response_model=TemplateUpdateResponse)
async def update_templates(
    req: TemplateUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    seen: set[tuple[str, str]] = set()
    for item in req.items:
        identity = (item.key, item.lang)
        if identity in seen:
            raise HTTPException(
                status_code=422,
                detail=f"Duplicate template in request: {item.key}/{item.lang}",
            )
        seen.add(identity)
        try:
            validate_template_content(item.key, item.lang, item.content)
        except TemplateValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Validate the complete request before mutating any ORM object so an
    # invalid item cannot leave earlier templates modified in the session.
    for item in req.items:
        existing = await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.key == item.key,
                MessageTemplate.lang == item.lang,
            )
        )
        tpl = existing.scalar_one_or_none()
        if tpl is None:
            session.add(
                MessageTemplate(
                    key=item.key,
                    lang=item.lang,
                    content=item.content,
                )
            )
        else:
            tpl.content = item.content
    await session.commit()
    if any(item.key == "payment_received" for item in req.items):
        await _reconcile_lots(
            request,
            "Message templates saved",
        )
    return TemplateUpdateResponse(updated=len(req.items))


@router.post(
    "/messages/{key}/{lang}/reset",
    response_model=TemplateOut,
)
async def reset_message_template(
    key: str,
    lang: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    default = DEFAULT_MESSAGE_TEMPLATES.get(key, {}).get(lang)
    if default is None:
        raise HTTPException(status_code=404, detail="Message template not found")
    template = (
        await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.key == key,
                MessageTemplate.lang == lang,
            )
        )
    ).scalar_one_or_none()
    if template is None:
        template = MessageTemplate(key=key, lang=lang, content=default)
        session.add(template)
    else:
        template.content = default
    await session.commit()
    if key == "payment_received":
        await _reconcile_lots(request, "Message template reset")
    return _message_template_out(template)


@router.get("/lot", response_model=list[LotTemplateOut])
async def list_lot_templates(session: AsyncSession = Depends(get_db_session)):
    rows = (
        await session.execute(
            select(LotTemplate)
            .outerjoin(LimitScope, LimitScope.id == LotTemplate.limit_scope_id)
            .where(
                (LotTemplate.limit_scope_id.is_(None))
                | (LimitScope.code.in_(("any", "codex")))
            )
            .order_by(
                LotTemplate.system_managed.desc(),
                LotTemplate.name,
                LotTemplate.id,
            )
        )
    ).scalars()
    return [_lot_template_out(row) for row in rows]


@router.post("/lot", response_model=LotTemplateOut, status_code=201)
async def create_lot_template(
    req: LotTemplateCreate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        key = validate_lot_template_key(req.key)
        validate_lot_template_values(
            title_ru=req.title_ru,
            title_en=req.title_en,
            description_ru=req.description_ru,
            description_en=req.description_en,
        )
    except LotTemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _validate_lot_template_targets(
        session, req.tier_id, req.limit_scope_id,
    )
    same_target = (
        await session.execute(
            select(LotTemplate).where(
                LotTemplate.system_managed.is_(False),
                (
                    LotTemplate.tier_id == req.tier_id
                    if req.tier_id is not None
                    else LotTemplate.tier_id.is_(None)
                ),
                (
                    LotTemplate.limit_scope_id == req.limit_scope_id
                    if req.limit_scope_id is not None
                    else LotTemplate.limit_scope_id.is_(None)
                ),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if same_target is not None:
        raise HTTPException(
            status_code=409,
            detail="A custom lot template already exists for this target",
        )
    row = LotTemplate(
        key=key,
        name=req.name,
        tier_id=req.tier_id,
        limit_scope_id=req.limit_scope_id,
        title_template_ru=req.title_ru,
        title_template_en=req.title_en,
        description_template_ru=req.description_ru,
        description_template_en=req.description_en,
        is_enabled=req.enabled,
        system_managed=False,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Lot template key or active target already exists",
        ) from exc
    await session.refresh(row)
    await _reconcile_lots(request, "Lot template created")
    return _lot_template_out(row)


@router.put("/lot/{key}", response_model=LotTemplateOut)
async def update_lot_template(
    key: str,
    req: LotTemplateUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    row = await _get_lot_template(session, key)
    if row.system_managed and not req.enabled:
        raise HTTPException(
            status_code=422,
            detail="The system default lot template must remain enabled",
        )
    try:
        validate_lot_template_values(
            title_ru=req.title_ru,
            title_en=req.title_en,
            description_ru=req.description_ru,
            description_en=req.description_en,
        )
    except LotTemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row.title_template_ru = req.title_ru
    row.title_template_en = req.title_en
    row.description_template_ru = req.description_ru
    row.description_template_en = req.description_en
    row.is_enabled = req.enabled
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Another active lot template already exists for this target",
        ) from exc
    await _reconcile_lots(request, "Lot template saved")
    return _lot_template_out(row)


@router.post("/lot/{key}/reset", response_model=LotTemplateOut)
async def reset_lot_template(
    key: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    row = await _get_lot_template(session, key)
    default = DEFAULT_LOT_TEMPLATES.get(row.key)
    if default is None:
        raise HTTPException(
            status_code=409,
            detail="Custom lot templates do not have a bundled default",
        )
    row.title_template_ru = default.title_ru
    row.title_template_en = default.title_en
    row.description_template_ru = default.description_ru
    row.description_template_en = default.description_en
    row.is_enabled = True
    await session.commit()
    await _reconcile_lots(request, "Lot template reset")
    return _lot_template_out(row)


@router.delete("/lot/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lot_template(
    key: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    row = await _get_lot_template(session, key)
    if row.system_managed:
        raise HTTPException(
            status_code=409,
            detail="System lot templates can be reset but not deleted",
        )
    await session.delete(row)
    await session.commit()
    await _reconcile_lots(request, "Lot template deleted")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _message_template_out(template: MessageTemplate) -> TemplateOut:
    default = DEFAULT_MESSAGE_TEMPLATES.get(template.key, {}).get(template.lang)
    try:
        allowed = sorted(allowed_template_fields(template.key, template.lang))
    except TemplateValidationError:
        # Legacy unknown rows remain visible for recovery but cannot be saved
        # until they are converted to a supported key.
        allowed = []
    return TemplateOut(
        key=template.key,
        lang=template.lang,
        content=template.content,
        allowed_fields=allowed,
        default_content=default,
        is_custom=default is None or template.content != default,
    )


def _lot_template_out(template: LotTemplate) -> LotTemplateOut:
    default = DEFAULT_LOT_TEMPLATES.get(template.key)
    values = (
        template.title_template_ru,
        template.title_template_en,
        template.description_template_ru,
        template.description_template_en,
    )
    defaults = (
        default.title_ru,
        default.title_en,
        default.description_ru,
        default.description_en,
    ) if default else None
    return LotTemplateOut(
        id=template.id,
        key=template.key,
        name=template.name,
        tier_id=template.tier_id,
        limit_scope_id=template.limit_scope_id,
        title_ru=template.title_template_ru,
        title_en=template.title_template_en,
        description_ru=template.description_template_ru,
        description_en=template.description_template_en,
        enabled=template.is_enabled,
        system_managed=template.system_managed,
        is_custom=defaults is None or values != defaults or not template.is_enabled,
        default_title_ru=default.title_ru if default else None,
        default_title_en=default.title_en if default else None,
        default_description_ru=default.description_ru if default else None,
        default_description_en=default.description_en if default else None,
        allowed_fields=sorted(LOT_TEMPLATE_FIELDS),
    )


async def _get_lot_template(
    session: AsyncSession, key: str,
) -> LotTemplate:
    row = (
        await session.execute(
            select(LotTemplate).where(LotTemplate.key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Lot template not found")
    return row


async def _validate_lot_template_targets(
    session: AsyncSession,
    tier_id: int | None,
    limit_scope_id: int | None,
) -> None:
    if tier_id is not None and await session.get(SubscriptionTier, tier_id) is None:
        raise HTTPException(status_code=422, detail="Unknown subscription tier")
    if (
        limit_scope_id is not None
    ):
        scope = await session.get(LimitScope, limit_scope_id)
        if (
            scope is None
            or scope.code not in {"any", "codex"}
            or not scope.is_enabled
        ):
            raise HTTPException(
                status_code=422,
                detail="Limit scope is disabled or unavailable",
            )


async def _reconcile_lots(request: Request, saved_message: str) -> None:
    """Publish template changes immediately after their database commit."""

    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is None:
        return
    try:
        # Template content is part of the remote FunPay offer form. Force a
        # full refresh even when price/capacity fields did not change.
        await lifecycle.reconcile_lots(refresh_published=True)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{saved_message}, but FunPay reconciliation failed",
        ) from exc
