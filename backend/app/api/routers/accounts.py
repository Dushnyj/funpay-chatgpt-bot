from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.capacity import notify_capacity_changed
from app.api.deps import get_current_user, get_db_session
from app.api.schemas import (
    AccountCreate,
    AccountCredentialsUpdate,
    AccountLimitsOut,
    AccountOut,
    AccountUpdate,
    AccountWithLimits,
    DeviceAuthStartOut,
    DeviceAuthStatusOut,
    ValidationJobOut,
)
from app.check_job_queue import ActiveJobConflict, CheckJobQueue
from app.models.account import (
    Account,
    AccountCheckJob,
    AccountLimits,
    EmailOAuthCredential,
)
from app.models.audit import AuditLog
from app.services.account_device_auth import (
    AccountBusyError,
    account_device_auth_manager,
)
from app.services.account_occupancy import (
    account_is_busy,
    active_rental_counts,
    replacement_reserved_account_ids,
)
from app.services.totp import generate_totp_at, is_valid_base32
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


def _account_out(
    account: Account,
    job: AccountCheckJob | None = None,
    email_oauth: EmailOAuthCredential | None = None,
    active_rentals_count: int = 0,
    replacement_reserved: bool = False,
) -> AccountOut:
    base = AccountOut.model_validate(account)
    return base.model_copy(
        update={
            "validation_job": _validation_job_out(job),
            "email_oauth_connected": (
                email_oauth is not None and email_oauth.status == "connected"
            ),
            "email_oauth_provider": (
                email_oauth.provider if email_oauth is not None else None
            ),
            "email_oauth_status": (
                email_oauth.status if email_oauth is not None else None
            ),
            "active_rentals_count": active_rentals_count,
            "replacement_reserved": replacement_reserved,
        }
    )


def _account_with_limits(
    account: Account,
    limits: AccountLimits | None,
    job: AccountCheckJob | None = None,
    email_oauth: EmailOAuthCredential | None = None,
    active_rentals_count: int = 0,
    replacement_reserved: bool = False,
) -> AccountWithLimits:
    base = _account_out(
        account,
        job,
        email_oauth,
        active_rentals_count,
        replacement_reserved,
    )
    return AccountWithLimits(
        **base.model_dump(),
        limits=(
            AccountLimitsOut.model_validate(limits)
            if limits is not None
            else None
        ),
    )


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


async def _limits_by_account(
    session: AsyncSession, account_ids: list[int],
) -> dict[int, AccountLimits]:
    if not account_ids:
        return {}
    result = await session.execute(
        select(AccountLimits).where(AccountLimits.account_id.in_(account_ids))
    )
    return {limits.account_id: limits for limits in result.scalars()}


async def _email_oauth_by_account(
    session: AsyncSession, account_ids: list[int],
) -> dict[int, EmailOAuthCredential]:
    if not account_ids:
        return {}
    result = await session.execute(
        select(EmailOAuthCredential).where(
            EmailOAuthCredential.account_id.in_(account_ids)
        )
    )
    return {credential.account_id: credential for credential in result.scalars()}


async def _active_rental_counts(
    session: AsyncSession, account_ids: list[int],
) -> dict[int, int]:
    return await active_rental_counts(session, account_ids)


async def _active_rental_count(session: AsyncSession, account_id: int) -> int:
    counts = await _active_rental_counts(session, [account_id])
    return counts.get(account_id, 0)


@router.get("", response_model=list[AccountWithLimits])
async def list_accounts(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Account).order_by(Account.id))
    accounts = list(result.scalars().all())
    account_ids = [account.id for account in accounts]
    jobs = await _latest_jobs(session, account_ids)
    limits = await _limits_by_account(session, account_ids)
    email_oauth = await _email_oauth_by_account(session, account_ids)
    active_rental_counts = await _active_rental_counts(session, account_ids)
    replacement_reserved_ids = await replacement_reserved_account_ids(
        session, account_ids,
    )
    return [
        _account_with_limits(
            account,
            limits.get(account.id),
            jobs.get(account.id),
            email_oauth.get(account.id),
            active_rental_counts.get(account.id, 0),
            account.id in replacement_reserved_ids,
        )
        for account in accounts
    ]


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(req: AccountCreate, session: AsyncSession = Depends(get_db_session)):
    account = Account(
        login=req.login,
        password_encrypted=req.password,
        totp_secret_encrypted=req.totp_secret,
        email=req.email,
        email_password_encrypted=req.email_password,
        tier_id=None,
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
    email_oauth = await session.get(EmailOAuthCredential, account.id)
    active_rental_counts = await _active_rental_counts(session, [account.id])
    replacement_reserved_ids = await replacement_reserved_account_ids(
        session, [account.id],
    )
    return _account_with_limits(
        account,
        limits,
        jobs.get(account.id),
        email_oauth,
        active_rental_counts.get(account.id, 0),
        account.id in replacement_reserved_ids,
    )


@router.post("/{account_id}/recheck", response_model=AccountOut, status_code=202)
async def recheck_account(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    # Serialize manual state changes with allocation and credential repair.
    account = (
        await session.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if await account_is_busy(session, account.id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Account validation cannot be restarted while an active or "
                "expiring rental occupies it"
            ),
        )
    try:
        job = await _check_job_queue.enqueue_exclusive(
            session,
            account.id,
            priority="manual",
            job_type="full_validation",
            superseded_by="manual_recheck",
        )
    except ActiveJobConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Validation job {exc.job_type} is already running",
        ) from exc
    account.operator_status_override = None
    account.status = "pending_validation"
    session.add(
        AuditLog(
            event_type="account_validation_requeued",
            account_id=account.id,
            metadata_={"actor": "admin", "job_id": job.id},
        )
    )
    await session.commit()
    notify_capacity_changed(request)
    await session.refresh(account)
    await session.refresh(job)
    email_oauth = await session.get(EmailOAuthCredential, account.id)
    active_rentals_count = await _active_rental_count(session, account.id)
    replacement_reserved = bool(
        await replacement_reserved_account_ids(session, [account.id])
    )
    return _account_out(
        account,
        job,
        email_oauth,
        active_rentals_count,
        replacement_reserved,
    )


@router.post(
    "/{account_id}/device-auth",
    response_model=DeviceAuthStartOut,
    status_code=201,
)
async def start_device_auth(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    # This is only a fast preflight. The manager releases the transaction for
    # remote device-code creation, then locks and rechecks before mutation.
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if await account_is_busy(session, account.id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Browser authorization cannot start while an active or "
                "expiring rental occupies the account"
            ),
        )
    try:
        auth_session = await account_device_auth_manager.start(session, account)
    except KeyError:
        raise HTTPException(status_code=404, detail="Account not found")
    except AccountBusyError:
        raise HTTPException(
            status_code=409,
            detail=(
                "Browser authorization cannot start while the account is "
                "occupied by a rental or reserved for replacement"
            ),
        )
    except ActiveJobConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Validation job {exc.job_type} is already running",
        ) from exc
    except DeviceAuthError as exc:
        raise HTTPException(
            status_code=503,
            detail="OpenAI device authorization is temporarily unavailable",
        ) from exc
    notify_capacity_changed(request)
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
    email_oauth = await session.get(EmailOAuthCredential, account.id)
    active_rentals_count = (
        await _active_rental_count(session, account.id)
        if auth_session.status == "completed"
        else 0
    )
    replacement_reserved = (
        bool(await replacement_reserved_account_ids(session, [account.id]))
        if auth_session.status == "completed"
        else False
    )
    return DeviceAuthStatusOut(
        status=auth_session.status,
        error_code=auth_session.error_code,
        error_detail=auth_session.error_detail,
        account=(
            _account_out(
                account,
                jobs.get(account.id),
                email_oauth,
                active_rentals_count,
                replacement_reserved,
            )
            if auth_session.status == "completed"
            else None
        ),
    )


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: int,
    req: AccountUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    account = (
        await session.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    changes = req.model_dump(exclude_unset=True)
    protected_changes = set(changes) - {"notes"}
    if protected_changes and await account_is_busy(session, account.id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Only operator notes can be changed while the account is "
                "occupied by a rental or reserved for replacement"
            ),
        )
    for field, value in changes.items():
        setattr(account, field, value)
    if "status" in changes:
        account.operator_status_override = changes["status"]
    await session.commit()
    if "status" in changes:
        notify_capacity_changed(request)
    await session.refresh(account)
    jobs = await _latest_jobs(session, [account.id])
    email_oauth = await session.get(EmailOAuthCredential, account.id)
    active_rentals_count = await _active_rental_count(session, account.id)
    replacement_reserved = bool(
        await replacement_reserved_account_ids(session, [account.id])
    )
    return _account_out(
        account,
        jobs.get(account.id),
        email_oauth,
        active_rentals_count,
        replacement_reserved,
    )


@router.patch(
    "/{account_id}/credentials",
    response_model=AccountOut,
    status_code=202,
)
async def update_account_credentials(
    account_id: int,
    req: AccountCredentialsUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Replace write-only credentials and force a fresh validation pass."""

    # The Account row is also the allocator's serialization primitive. Lock it
    # before checking rentals so a concurrent purchase cannot appear between
    # the guard and the credential update commit.
    account = (
        await session.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if await account_is_busy(session, account_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Account credentials cannot be changed while it is occupied "
                "by a rental or reserved for replacement"
            ),
        )
    email_oauth = await session.get(EmailOAuthCredential, account.id)

    changes = req.model_dump(exclude_unset=True)
    if changes.get("login", ...) is None:
        raise HTTPException(status_code=422, detail="Login cannot be cleared")
    if changes.get("password", ...) is None:
        raise HTTPException(status_code=422, detail="Password cannot be cleared")
    for field, limit in (
        ("password", 4096),
        ("totp_secret", 256),
        ("email_password", 4096),
    ):
        value = changes.get(field)
        if value is not None and (not value or len(value) > limit):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {field.replace('_', ' ')} value",
            )

    resulting_email = changes.get("email", account.email)
    if changes.get("email_password") is not None and resulting_email is None:
        raise HTTPException(
            status_code=422,
            detail="Email password requires an email address",
        )

    changed_fields = set(changes)
    if "login" in changes:
        account.login = changes["login"]
    if "password" in changes:
        account.password_encrypted = changes["password"]
    if "totp_secret" in changes:
        account.totp_secret_encrypted = changes["totp_secret"] or ""

    old_email = account.email.strip().casefold() if account.email else None
    new_email = resulting_email.strip().casefold() if resulting_email else None
    email_changed = old_email != new_email
    if "email" in changes:
        account.email = changes["email"]
    if "email_password" in changes:
        account.email_password_encrypted = changes["email_password"]
    elif email_changed:
        # A password for the previous mailbox must never be tried against a
        # newly supplied address.
        account.email_password_encrypted = None
        changed_fields.add("email_password")

    if email_changed and email_oauth is not None:
        await session.delete(email_oauth)
        email_oauth = None
        changed_fields.add("email_oauth")

    # Surface a duplicate login before queue queries can trigger an implicit
    # autoflush. The final commit remains guarded for a concurrent duplicate.
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Login already exists")

    account.operator_status_override = None
    account.status = "pending_validation"
    try:
        job = await _check_job_queue.enqueue_exclusive(
            session,
            account.id,
            priority="manual",
            job_type="full_validation",
            superseded_by="credential_repair",
        )
    except ActiveJobConflict as exc:
        # The running worker owns a snapshot of the previous credentials. Let
        # it finish, but require exactly one follow-up validation afterward.
        account.validation_rerun_requested = True
        job = await session.get(AccountCheckJob, exc.job_id)
        if job is None:  # Defensive: the queue row is durable in normal use.
            await session.rollback()
            raise HTTPException(status_code=409, detail="Validation is already running")
        rerun_requested = True
    else:
        account.validation_rerun_requested = False
        rerun_requested = False

    session.add(
        AuditLog(
            event_type="account_credentials_updated",
            account_id=account.id,
            metadata_={
                "actor": "admin",
                "changed_fields": sorted(changed_fields),
                "job_id": job.id,
                "rerun_requested": rerun_requested,
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Login already exists")

    notify_capacity_changed(request)
    await session.refresh(account)
    await session.refresh(job)
    active_rentals_count = await _active_rental_count(session, account.id)
    replacement_reserved = bool(
        await replacement_reserved_account_ids(session, [account.id])
    )
    return _account_out(
        account,
        job,
        email_oauth,
        active_rentals_count,
        replacement_reserved,
    )


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    account = (
        await session.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if await account_is_busy(session, account_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Account is occupied by a rental or reserved for replacement"
            ),
        )
    await session.delete(account)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Account is referenced by history")
    notify_capacity_changed(request)


class TotpExportResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_png_base64: str


class TotpCodeResponse(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
    seconds_remaining: int = Field(ge=1, le=30)


_TOTP_STEP_SECONDS = 30
_TOTP_MIN_RESPONSE_VALIDITY_SECONDS = 5
_TOTP_TRANSPORT_SAFETY_SECONDS = 2


def _now() -> float:
    """Local clock seam for deterministic TOTP boundary tests."""
    return time.time()


async def _sleep(seconds: float) -> None:
    """Local async wait seam paired with ``_now``."""
    await asyncio.sleep(seconds)


@router.get("/{account_id}/totp-code", response_model=TotpCodeResponse)
async def get_totp_code(
    account_id: int,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
):
    """Generate a current TOTP without exposing the reusable setup secret."""
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    secret = account.totp_secret_encrypted or ""
    if not secret:
        raise HTTPException(status_code=400, detail="Account has no TOTP secret")
    if not is_valid_base32(secret):
        raise HTTPException(status_code=400, detail="Account has an invalid TOTP secret")

    session.add(
        AuditLog(
            event_type="totp_code_generated",
            account_id=account.id,
            metadata_={"actor": "admin"},
        )
    )
    await session.commit()

    # Generate only after the audit transaction is durable, otherwise a slow
    # commit can consume the final seconds of the code.  At the window edge we
    # wait for the next code and report a conservative validity interval so a
    # normal network round-trip cannot leave the UI copying an expired value.
    timestamp = _now()
    raw_remaining = _TOTP_STEP_SECONDS - (
        int(timestamp) % _TOTP_STEP_SECONDS
    )
    if raw_remaining <= _TOTP_MIN_RESPONSE_VALIDITY_SECONDS:
        await _sleep(raw_remaining + 0.05)
        timestamp = _now()
        raw_remaining = _TOTP_STEP_SECONDS - (
            int(timestamp) % _TOTP_STEP_SECONDS
        )
    code = generate_totp_at(secret, timestamp)
    seconds_remaining = max(
        1,
        raw_remaining - _TOTP_TRANSPORT_SAFETY_SECONDS,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TotpCodeResponse(code=code, seconds_remaining=seconds_remaining)


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

