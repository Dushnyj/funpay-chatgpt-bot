from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import (
    AccountCreate,
    AccountLimitsOut,
    AccountOut,
    AccountUpdate,
    AccountWithLimits,
    DeviceAuthStartOut,
    DeviceAuthStatusOut,
    ValidationJobOut,
)
from app.check_job_queue import CheckJobQueue
from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.audit import AuditLog
from app.models.rental import Rental
from app.services.account_device_auth import account_device_auth_manager
from app.integrations.openai.device_auth import DeviceAuthError

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(get_current_user)])
_check_job_queue = CheckJobQueue()


class BulkAccountItem(BaseModel):
    login: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=4096)
    totp_secret: str = Field(default="", max_length=256)
    email: str | None = Field(default=None, max_length=320)
    email_password: str | None = Field(default=None, max_length=4096)


class BulkAccountRequest(BaseModel):
    accounts: list[BulkAccountItem] = Field(min_length=1, max_length=1000)


class BulkAccountResponse(BaseModel):
    created: int


def _validation_job_out(job: AccountCheckJob | None) -> ValidationJobOut | None:
    if job is None:
        return None
    stage = error_code = error_detail = None
    if job.error:
        try:
            payload = json.loads(job.error)
        except (TypeError, ValueError):
            error_code = job.error[:120]
        else:
            if isinstance(payload, dict):
                stage = str(payload.get("stage")) if payload.get("stage") else None
                error_code = str(payload.get("code")) if payload.get("code") else None
                error_detail = str(payload.get("detail"))[:1000] if payload.get("detail") else None
    return ValidationJobOut(
        id=job.id,
        status=job.status,
        job_type=job.job_type,
        priority=job.priority,
        stage=stage,
        error_code=error_code,
        error_detail=error_detail,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _account_out(account: Account, job: AccountCheckJob | None = None) -> AccountOut:
    base = AccountOut.model_validate(account)
    return base.model_copy(update={"validation_job": _validation_job_out(job)})


async def _latest_jobs(
    session: AsyncSession, account_ids: list[int],
) -> dict[int, AccountCheckJob]:
    if not account_ids:
        return {}
    result = await session.execute(
        select(AccountCheckJob)
        .where(AccountCheckJob.account_id.in_(account_ids))
        .order_by(AccountCheckJob.account_id, AccountCheckJob.id.desc())
    )
    latest: dict[int, AccountCheckJob] = {}
    for job in result.scalars():
        latest.setdefault(job.account_id, job)
    return latest


@router.get("", response_model=list[AccountOut])
async def list_accounts(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Account).order_by(Account.id))
    accounts = list(result.scalars().all())
    jobs = await _latest_jobs(session, [account.id for account in accounts])
    return [_account_out(account, jobs.get(account.id)) for account in accounts]


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(req: AccountCreate, session: AsyncSession = Depends(get_db_session)):
    account = Account(
        login=req.login,
        password_encrypted=req.password,
        totp_secret_encrypted=req.totp_secret,
        email=req.email,
        email_password_encrypted=req.email_password,
        tier_id=None,
        subscription_expires_at=req.subscription_expires_at,
        max_active_rentals=req.max_active_rentals,
        notes=req.notes,
    )
    session.add(account)
    try:
        await session.flush()
        job = await _check_job_queue.enqueue(
            session, account.id, priority="new", job_type="full_validation"
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Login already exists")
    await session.refresh(account)
    await session.refresh(job)
    return _account_out(account, job)


@router.post("/bulk", response_model=BulkAccountResponse, status_code=201)
async def bulk_add_accounts(req: BulkAccountRequest, session: AsyncSession = Depends(get_db_session)):
    accounts: list[Account] = []
    for item in req.accounts:
        account = Account(
            login=item.login,
            password_encrypted=item.password,
            totp_secret_encrypted=item.totp_secret,
            email=item.email,
            email_password_encrypted=item.email_password,
            tier_id=None,
            status="pending_validation",
        )
        session.add(account)
        accounts.append(account)
    try:
        await session.flush()
        for account in accounts:
            await _check_job_queue.enqueue(
                session, account.id, priority="new", job_type="full_validation"
            )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Duplicate login")
    return BulkAccountResponse(created=len(req.accounts))


@router.get("/{account_id}", response_model=AccountWithLimits)
async def get_account(account_id: int, session: AsyncSession = Depends(get_db_session)):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    limits = await session.get(AccountLimits, account_id)
    jobs = await _latest_jobs(session, [account.id])
    base = _account_out(account, jobs.get(account.id))
    return AccountWithLimits(
        **base.model_dump(),
        limits=AccountLimitsOut.model_validate(limits) if limits is not None else None,
    )


@router.post("/{account_id}/recheck", response_model=AccountOut, status_code=202)
async def recheck_account(
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    account.status = "pending_validation"
    job = await _check_job_queue.enqueue(
        session, account.id, priority="manual", job_type="full_validation"
    )
    session.add(
        AuditLog(
            event_type="account_validation_requeued",
            account_id=account.id,
            metadata_={"actor": "admin", "job_id": job.id},
        )
    )
    await session.commit()
    await session.refresh(account)
    await session.refresh(job)
    return _account_out(account, job)


@router.post(
    "/{account_id}/device-auth",
    response_model=DeviceAuthStartOut,
    status_code=201,
)
async def start_device_auth(
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        auth_session = await account_device_auth_manager.start(session, account)
    except DeviceAuthError as exc:
        raise HTTPException(
            status_code=503,
            detail="OpenAI device authorization is temporarily unavailable",
        ) from exc
    assert auth_session.code is not None
    return DeviceAuthStartOut(
        session_id=auth_session.id,
        verification_url=auth_session.code.verification_url,
        user_code=auth_session.code.user_code,
        expires_at=auth_session.expires_at,
        interval_seconds=auth_session.code.interval_seconds,
    )


@router.get(
    "/{account_id}/device-auth/{session_id}",
    response_model=DeviceAuthStatusOut,
)
async def get_device_auth_status(
    account_id: int,
    session_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        auth_session = await account_device_auth_manager.poll(
            session,
            account,
            session_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Device authorization not found")
    jobs = await _latest_jobs(session, [account.id])
    return DeviceAuthStatusOut(
        status=auth_session.status,
        error_code=auth_session.error_code,
        error_detail=auth_session.error_detail,
        account=(
            _account_out(account, jobs.get(account.id))
            if auth_session.status == "completed"
            else None
        ),
    )


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(account_id: int, req: AccountUpdate, session: AsyncSession = Depends(get_db_session)):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    await session.commit()
    await session.refresh(account)
    jobs = await _latest_jobs(session, [account.id])
    return _account_out(account, jobs.get(account.id))


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, session: AsyncSession = Depends(get_db_session)):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    active_rental = await session.scalar(
        select(Rental.id).where(
            Rental.account_id == account_id,
            Rental.status == "active",
        ).limit(1)
    )
    if active_rental is not None:
        raise HTTPException(status_code=409, detail="Account has an active rental")
    await session.delete(account)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Account is referenced by history")


class TotpExportResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_png_base64: str


@router.get("/{account_id}/totp-export", response_model=TotpExportResponse)
async def export_totp(
    account_id: int,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
):
    """Экспорт TOTP: secret, otpauth:// URI, QR-код (base64 PNG) для импорта в приложение."""
    from app.services.otp_export import generate_otpauth_uri, generate_qr_base64
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    secret = account.totp_secret_encrypted or ""
    if not secret:
        raise HTTPException(status_code=400, detail="Account has no TOTP secret")

    account_name = account.email or account.login
    uri = generate_otpauth_uri(secret, account_name)
    qr_b64 = generate_qr_base64(uri)
    session.add(
        AuditLog(
            event_type="totp_export",
            account_id=account.id,
            metadata_={"actor": "admin"},
        )
    )
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TotpExportResponse(secret=secret, otpauth_uri=uri, qr_png_base64=qr_b64)

