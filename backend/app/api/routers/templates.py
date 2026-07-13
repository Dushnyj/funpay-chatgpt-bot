from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import TemplateOut, TemplateUpdate
from app.models.message import MessageTemplate
from app.services.messages import TemplateValidationError, validate_template_content

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
    return result.scalars().all()


@router.put("", response_model=TemplateUpdateResponse)
async def update_templates(
    req: TemplateUpdate,
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
    return TemplateUpdateResponse(updated=len(req.items))
