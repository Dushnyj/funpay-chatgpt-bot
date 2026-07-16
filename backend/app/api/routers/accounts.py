from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.capacity import notify_capacity_changed, notify_validation_queued
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
from app.models.proxy_route import ProxyRoute
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
from app.services.proxy_routes import proxy_route_check_is_fresh
from app.integrations.openai.device_auth import DeviceAuthError

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(get_current_user)])
_check_job_queue = CheckJobQueue()
_MANUAL_BROWSER_CONFIRMATION_MAX_AGE = timedelta(minutes=30)


@dataclass(frozen=True)
class _ManualBrowserConfirmationEvidence:
    available: bool = False
    expires_at: datetime | None = None
    validation_job: AccountCheckJob | None = None
    device_auth_job: AccountCheckJob | None = None
    rejection_status: int = 409
    rejection_detail: str = "Аккаунт не ожидает ручного подтверждения входа."


class BulkAccountItem(BaseModel):
    login: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=4096)
    totp_secret: str = Field(default="", max_length=256)
    email: str | None = Field(default=None, max_length=320)
    email_password: str | None = Field(default=None, max_length=4096)
    proxy_route_id: int | None = Field(default=None, gt=0)


class BulkAccountRequest(BaseModel):
    accounts: list[BulkAccountItem] = Field(min_length=1, max_length=1000)


class BulkAccountResponse(BaseModel):
    created: int


async def _validate_proxy_route_reference(
    session: AsyncSession,
    route_id: int | None,
) -> None:
    if route_id is None:
        return
    # Serialize assignment with home-relay re-pair/delete and connection
    # changes.  Reading an old online snapshot and waiting only on the FK
    # write could otherwise attach an account after the route became unchecked.
    route = (
        await session.execute(
            select(ProxyRoute)
            .where(ProxyRoute.id == route_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if route is None:
        raise HTTPException(status_code=422, detail="Proxy route not found")
    if (
        not route.enabled
        or route.status != "online"
        or not proxy_route_check_is_fresh(route.last_checked_at)
    ):
        raise HTTPException(
            status_code=422,
            detail="Proxy route must have a fresh online test before assignment",
        )


async def _lock_active_account_check(
    session: AsyncSession,
    account_id: int,
) -> AccountCheckJob | None:
    """Lock the durable check lease after the caller locked Account."""

    return (
        await session.execute(
            select(AccountCheckJob)
            .where(
                AccountCheckJob.account_id == account_id,
                AccountCheckJob.status.in_(("pending", "running")),
            )
            .order_by(AccountCheckJob.id)
            .with_for_update()
        )
    ).scalars().first()


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


def _job_error_code(job: AccountCheckJob | None) -> str | None:
    if job is None or not job.error:
        return None
    try:
        payload = json.loads(job.error)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("code"):
        return None
    return str(payload["code"])


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _account_out(
    account: Account,
    job: AccountCheckJob | None = None,
    email_oauth: EmailOAuthCredential | None = None,
    active_rentals_count: int = 0,
    replacement_reserved: bool = False,
    manual_browser_confirmation: _ManualBrowserConfirmationEvidence | None = None,
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
            "manual_browser_confirmation_available": bool(
                manual_browser_confirmation is not None
                and manual_browser_confirmation.available
            ),
            "manual_browser_confirmation_expires_at": (
                manual_browser_confirmation.expires_at
                if manual_browser_confirmation is not None
                and manual_browser_confirmation.available
                else None
            ),
        }
    )


def _account_with_limits(
    account: Account,
    limits: AccountLimits | None,
    job: AccountCheckJob | None = None,
    email_oauth: EmailOAuthCredential | None = None,
    active_rentals_count: int = 0,
    replacement_reserved: bool = False,
    manual_browser_confirmation: _ManualBrowserConfirmationEvidence | None = None,
) -> AccountWithLimits:
    base = _account_out(
        account,
        job,
        email_oauth,
        active_rentals_count,
        replacement_reserved,
        manual_browser_confirmation,
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
    session: AsyncSession,
    account_ids: list[int],
    *,
    for_update: bool = False,
) -> dict[int, AccountCheckJob]:
    if not account_ids:
        return {}
    latest_ids = (
        select(func.max(AccountCheckJob.id))
        .where(
            AccountCheckJob.account_id.in_(account_ids),
            AccountCheckJob.job_type != "limit_check",
        )
        .group_by(AccountCheckJob.account_id)
    )
    statement = select(AccountCheckJob).where(AccountCheckJob.id.in_(latest_ids))
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    return {job.account_id: job for job in result.scalars()}


async def _limits_by_account(
    session: AsyncSession,
    account_ids: list[int],
    *,
    for_update: bool = False,
) -> dict[int, AccountLimits]:
    if not account_ids:
        return {}
    statement = select(AccountLimits).where(
        AccountLimits.account_id.in_(account_ids)
    )
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
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


def _manual_confirmation_rejected(
    detail: str,
    *,
    status_code: int = 409,
    validation_job: AccountCheckJob | None = None,
) -> _ManualBrowserConfirmationEvidence:
    return _ManualBrowserConfirmationEvidence(
        validation_job=validation_job,
        rejection_status=status_code,
        rejection_detail=detail,
    )


def _metadata_job_id(metadata: dict | None, key: str) -> int | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _manual_browser_confirmation_evidence(
    session: AsyncSession,
    accounts: list[Account],
    *,
    latest_jobs: dict[int, AccountCheckJob] | None = None,
    limits_by_account: dict[int, AccountLimits] | None = None,
    busy_account_ids: set[int] | None = None,
    now: datetime | None = None,
    for_update: bool = False,
) -> dict[int, _ManualBrowserConfirmationEvidence]:
    """Return the exact evidence accepted by manual browser confirmation.

    The list/detail API and the mutating confirmation endpoint deliberately use
    this same evaluator.  Background limit checks may occur after the linked
    Device Auth/full-validation pair, but any other intervening validation or
    credential operation invalidates the attestation chain.
    """

    if not accounts:
        return {}
    account_ids = [account.id for account in accounts]
    if latest_jobs is None:
        latest_jobs = await _latest_jobs(
            session, account_ids, for_update=for_update
        )
    if limits_by_account is None:
        limits_by_account = await _limits_by_account(
            session, account_ids, for_update=for_update
        )
    if busy_account_ids is None:
        rental_counts = await _active_rental_counts(session, account_ids)
        replacement_ids = await replacement_reserved_account_ids(
            session, account_ids
        )
        busy_account_ids = {
            account_id
            for account_id, count in rental_counts.items()
            if count > 0
        } | replacement_ids

    checked_at = _as_utc(now or datetime.now(timezone.utc))
    assert checked_at is not None
    cutoff = checked_at - _MANUAL_BROWSER_CONFIRMATION_MAX_AGE
    evidence: dict[int, _ManualBrowserConfirmationEvidence] = {}
    candidates: dict[int, Account] = {}

    for account in accounts:
        job = latest_jobs.get(account.id)
        if account.id in busy_account_ids:
            evidence[account.id] = _manual_confirmation_rejected(
                "Нельзя подтверждать вход, пока аккаунт занят арендой или "
                "зарезервирован для замены.",
                validation_job=job,
            )
        elif account.operator_status_override is not None:
            evidence[account.id] = _manual_confirmation_rejected(
                "Сначала снимите ручную приостановку аккаунта.",
                validation_job=job,
            )
        elif (
            account.status != "validation_failed"
            or account.validation_rerun_requested
        ):
            evidence[account.id] = _manual_confirmation_rejected(
                "Аккаунт не ожидает ручного подтверждения входа.",
                validation_job=job,
            )
        elif not isinstance(account.password_encrypted, str) or not account.password_encrypted.strip():
            evidence[account.id] = _manual_confirmation_rejected(
                "Сохранённый пароль недоступен.",
                status_code=422,
                validation_job=job,
            )
        elif not is_valid_base32(account.totp_secret_encrypted or ""):
            evidence[account.id] = _manual_confirmation_rejected(
                "Сохранённый TOTP setup key некорректен.",
                status_code=422,
                validation_job=job,
            )
        elif (
            job is None
            or job.job_type != "full_validation"
            or job.status != "failed"
            or _job_error_code(job) != "cloudflare_challenge"
        ):
            evidence[account.id] = _manual_confirmation_rejected(
                "Последняя полная проверка завершилась не только на Cloudflare.",
                validation_job=job,
            )
        else:
            candidates[account.id] = account

    if not candidates:
        return evidence

    candidate_ids = list(candidates)
    active_statement = select(AccountCheckJob.account_id).where(
        AccountCheckJob.account_id.in_(candidate_ids),
        AccountCheckJob.status.in_(("pending", "running")),
    )
    if for_update:
        active_statement = active_statement.with_for_update()
    active_result = await session.execute(active_statement)
    active_job_accounts = {int(account_id) for account_id in active_result.scalars()}

    audit_statement = (
        select(AuditLog)
        .where(
            AuditLog.account_id.in_(candidate_ids),
            AuditLog.event_type == "account_device_auth_completed",
            AuditLog.timestamp >= cutoff,
        )
        .order_by(AuditLog.account_id, AuditLog.id.desc())
    )
    if for_update:
        audit_statement = audit_statement.with_for_update()
    audit_result = await session.execute(audit_statement)
    completion_audits: dict[int, AuditLog] = {}
    for audit in audit_result.scalars():
        account_id = int(audit.account_id or 0)
        current_job = latest_jobs.get(account_id)
        if (
            account_id in completion_audits
            or current_job is None
            or _metadata_job_id(
                audit.metadata_, "credential_validation_job_id"
            ) != current_job.id
        ):
            continue
        completion_audits[account_id] = audit

    device_job_ids = {
        device_id
        for audit in completion_audits.values()
        if (device_id := _metadata_job_id(audit.metadata_, "job_id")) is not None
    }
    device_jobs: dict[int, AccountCheckJob] = {}
    if device_job_ids:
        device_statement = select(AccountCheckJob).where(
            AccountCheckJob.id.in_(device_job_ids)
        )
        if for_update:
            device_statement = device_statement.with_for_update()
        device_result = await session.execute(device_statement)
        device_jobs = {job.id: job for job in device_result.scalars()}

    credential_result = await session.execute(
        select(AuditLog.account_id, func.max(AuditLog.id))
        .where(
            AuditLog.account_id.in_(candidate_ids),
            AuditLog.event_type == "account_credentials_updated",
        )
        .group_by(AuditLog.account_id)
    )
    latest_credential_audit_ids = {
        int(account_id): int(audit_id)
        for account_id, audit_id in credential_result.all()
        if account_id is not None and audit_id is not None
    }

    linked_pairs: dict[int, tuple[AccountCheckJob, AccountCheckJob]] = {}
    for account_id in candidate_ids:
        audit = completion_audits.get(account_id)
        current_job = latest_jobs[account_id]
        device_id = _metadata_job_id(
            audit.metadata_ if audit is not None else None, "job_id"
        )
        device_job = device_jobs.get(device_id or -1)
        if device_job is not None:
            linked_pairs[account_id] = (device_job, current_job)

    intervening_non_limit_accounts: set[int] = set()
    if linked_pairs:
        min_device_id = min(device.id for device, _current in linked_pairs.values())
        max_validation_id = max(current.id for _device, current in linked_pairs.values())
        intervening_result = await session.execute(
            select(AccountCheckJob.account_id, AccountCheckJob.id).where(
                AccountCheckJob.account_id.in_(list(linked_pairs)),
                AccountCheckJob.job_type != "limit_check",
                AccountCheckJob.id > min_device_id,
                AccountCheckJob.id < max_validation_id,
            )
        )
        for account_id, job_id in intervening_result.all():
            pair = linked_pairs.get(int(account_id))
            if pair is None:
                continue
            device_job, current_job = pair
            if device_job.id < int(job_id) < current_job.id:
                intervening_non_limit_accounts.add(int(account_id))

    for account_id, account in candidates.items():
        current_job = latest_jobs[account_id]
        if account_id in active_job_accounts:
            evidence[account_id] = _manual_confirmation_rejected(
                "Новая проверка аккаунта ещё выполняется.",
                validation_job=current_job,
            )
            continue

        completion_audit = completion_audits.get(account_id)
        device_id = _metadata_job_id(
            completion_audit.metadata_ if completion_audit is not None else None,
            "job_id",
        )
        device_job = device_jobs.get(device_id or -1)
        device_finished_at = _as_utc(
            device_job.finished_at if device_job is not None else None
        )
        validation_finished_at = _as_utc(current_job.finished_at)
        audit_at = _as_utc(
            completion_audit.timestamp if completion_audit is not None else None
        )
        if (
            completion_audit is None
            or device_job is None
            or device_job.account_id != account_id
            or device_job.id >= current_job.id
            or device_job.job_type != "device_auth"
            or device_job.status != "done"
            or device_job.result != "tokens_connected"
            or device_finished_at is None
            or device_finished_at < cutoff
            or validation_finished_at is None
            or validation_finished_at < device_finished_at
            or audit_at is None
            or audit_at < device_finished_at
        ):
            evidence[account_id] = _manual_confirmation_rejected(
                "Нет свежего успешного подтверждения Device Auth.",
                validation_job=current_job,
            )
            continue
        if account_id in intervening_non_limit_accounts:
            evidence[account_id] = _manual_confirmation_rejected(
                "Цепочка Device Auth и проверки учётных данных изменилась.",
                validation_job=current_job,
            )
            continue
        if latest_credential_audit_ids.get(account_id, 0) > completion_audit.id:
            evidence[account_id] = _manual_confirmation_rejected(
                "Цепочка Device Auth и проверки учётных данных изменилась.",
                validation_job=current_job,
            )
            continue

        limits = limits_by_account.get(account_id)
        measured_at = _as_utc(limits.measured_at if limits is not None else None)
        plan_detected_at = _as_utc(account.plan_detected_at)
        if (
            limits is None
            or measured_at is None
            or measured_at < cutoff
            or limits.refresh_status != "ok"
            or limits.plan_window_status != "ok"
            or not limits.plan_type
            or limits.plan_type == "unknown"
            or not limits.expected_long_window_seconds
            or not limits.refresh_token_encrypted
            or not limits.access_token_encrypted
            or account.tier_id is None
            or plan_detected_at is None
            or plan_detected_at < cutoff
        ):
            evidence[account_id] = _manual_confirmation_rejected(
                "Нет свежих подтверждённых данных тарифа и лимита.",
                validation_job=current_job,
            )
            continue

        expires_at = min(
            device_finished_at + _MANUAL_BROWSER_CONFIRMATION_MAX_AGE,
            audit_at + _MANUAL_BROWSER_CONFIRMATION_MAX_AGE,
            measured_at + _MANUAL_BROWSER_CONFIRMATION_MAX_AGE,
            plan_detected_at + _MANUAL_BROWSER_CONFIRMATION_MAX_AGE,
        )
        if expires_at <= checked_at:
            evidence[account_id] = _manual_confirmation_rejected(
                "Нет свежего успешного подтверждения Device Auth.",
                validation_job=current_job,
            )
            continue
        evidence[account_id] = _ManualBrowserConfirmationEvidence(
            available=True,
            expires_at=expires_at,
            validation_job=current_job,
            device_auth_job=device_job,
            rejection_detail="",
        )

    return evidence


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
    busy_account_ids = {
        account_id
        for account_id, count in active_rental_counts.items()
        if count > 0
    } | replacement_reserved_ids
    manual_confirmations = await _manual_browser_confirmation_evidence(
        session,
        accounts,
        latest_jobs=jobs,
        limits_by_account=limits,
        busy_account_ids=busy_account_ids,
    )
    return [
        _account_with_limits(
            account,
            limits.get(account.id),
            jobs.get(account.id),
            email_oauth.get(account.id),
            active_rental_counts.get(account.id, 0),
            account.id in replacement_reserved_ids,
            manual_confirmations.get(account.id),
        )
        for account in accounts
    ]


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(
    req: AccountCreate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    await _validate_proxy_route_reference(session, req.proxy_route_id)
    account = Account(
        login=req.login,
        password_encrypted=req.password,
        totp_secret_encrypted=req.totp_secret,
        email=req.email,
        email_password_encrypted=req.email_password,
        tier_id=None,
        max_active_rentals=req.max_active_rentals,
        notes=req.notes,
        proxy_route_id=req.proxy_route_id,
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
    notify_validation_queued(request)
    await session.refresh(account)
    await session.refresh(job)
    return _account_out(account, job)


@router.post("/bulk", response_model=BulkAccountResponse, status_code=201)
async def bulk_add_accounts(
    req: BulkAccountRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    accounts: list[Account] = []
    for item in req.accounts:
        await _validate_proxy_route_reference(session, item.proxy_route_id)
        account = Account(
            login=item.login,
            password_encrypted=item.password,
            totp_secret_encrypted=item.totp_secret,
            email=item.email,
            email_password_encrypted=item.email_password,
            tier_id=None,
            status="pending_validation",
            proxy_route_id=item.proxy_route_id,
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
    notify_validation_queued(request)
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
    busy_account_ids = (
        {account.id}
        if active_rental_counts.get(account.id, 0) > 0
        or account.id in replacement_reserved_ids
        else set()
    )
    manual_confirmations = await _manual_browser_confirmation_evidence(
        session,
        [account],
        latest_jobs=jobs,
        limits_by_account={account.id: limits} if limits is not None else {},
        busy_account_ids=busy_account_ids,
    )
    return _account_with_limits(
        account,
        limits,
        jobs.get(account.id),
        email_oauth,
        active_rental_counts.get(account.id, 0),
        account.id in replacement_reserved_ids,
        manual_confirmations.get(account.id),
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
    notify_validation_queued(request)
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
        notify_capacity_changed(request)
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
    request: Request,
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
    if auth_session.status in {"completed", "failed", "expired"}:
        notify_capacity_changed(request)
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


@router.post(
    "/{account_id}/confirm-browser-validation",
    response_model=AccountOut,
)
async def confirm_browser_validation(
    account_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Accept an operator-observed login only for the narrow Cloudflare case.

    Device Auth already attests identity, tokens, tier and usage data. This
    endpoint is the final manual acknowledgement that the stored password and
    TOTP were exercised successfully in a real browser when the server-side
    Playwright pass could not get beyond Cloudflare.
    """

    account = (
        await session.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    now = datetime.now(timezone.utc)
    busy_account_ids = (
        {account.id} if await account_is_busy(session, account.id) else set()
    )
    confirmation = (
        await _manual_browser_confirmation_evidence(
            session,
            [account],
            busy_account_ids=busy_account_ids,
            now=now,
            for_update=True,
        )
    )[account.id]
    if not confirmation.available:
        raise HTTPException(
            status_code=confirmation.rejection_status,
            detail=confirmation.rejection_detail,
        )
    current_job = confirmation.validation_job
    device_job = confirmation.device_auth_job
    assert current_job is not None and device_job is not None

    account.status = "active"
    account.chatgpt_last_check_at = now
    account.validation_rerun_requested = False
    session.add(
        AuditLog(
            event_type="account_browser_validation_confirmed",
            account_id=account.id,
            metadata_={
                "actor": "admin",
                "device_auth_job_id": device_job.id,
                "full_validation_job_id": current_job.id,
            },
        )
    )
    await session.commit()
    notify_capacity_changed(request)

    await session.refresh(account)
    email_oauth = await session.get(EmailOAuthCredential, account.id)
    active_rentals_count = await _active_rental_count(session, account.id)
    replacement_reserved = bool(
        await replacement_reserved_account_ids(session, [account.id])
    )
    return _account_out(
        account,
        current_job,
        email_oauth,
        active_rentals_count,
        replacement_reserved,
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
    proxy_route_changed = (
        "proxy_route_id" in changes
        and changes["proxy_route_id"] != account.proxy_route_id
    )
    if proxy_route_changed:
        active_job = await _lock_active_account_check(session, account.id)
        if active_job is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Proxy route cannot change while validation job "
                    f"{active_job.id} is {active_job.status}"
                ),
            )
    protected_changes = set(changes) - {"notes"}
    if protected_changes and await account_is_busy(session, account.id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Only operator notes can be changed while the account is "
                "occupied by a rental or reserved for replacement"
            ),
        )
    if proxy_route_changed:
        await _validate_proxy_route_reference(session, changes["proxy_route_id"])
    for field, value in changes.items():
        setattr(account, field, value)
    if "status" in changes:
        account.operator_status_override = changes["status"]
    route_validation_job: AccountCheckJob | None = None
    if proxy_route_changed:
        # A successful check only certifies the route it used. Never keep an
        # account sellable after switching even to another already-online
        # route; require a complete login/mail/limits pass on the new path.
        account.status = account.operator_status_override or "pending_validation"
        account.chatgpt_last_check_at = None
        account.validation_rerun_requested = False
        route_validation_job = await _check_job_queue.enqueue(
            session,
            account.id,
            priority="manual",
            job_type="full_validation",
        )
        session.add(
            AuditLog(
                event_type="account_proxy_route_changed",
                account_id=account.id,
                metadata_={
                    "actor": "admin",
                    "job_id": route_validation_job.id,
                },
            )
        )
    await session.commit()
    if "status" in changes or "proxy_route_id" in changes:
        notify_capacity_changed(request)
    if route_validation_job is not None:
        notify_validation_queued(request)
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

    notify_validation_queued(request)
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

