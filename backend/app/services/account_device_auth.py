from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openai.device_auth import (
    DeviceAuthError,
    DeviceCode,
    exchange_device_authorization,
    poll_device_authorization,
    request_device_code,
)
from app.integrations.openai.oauth import parse_id_token
from app.models.account import Account, AccountCheckJob
from app.models.audit import AuditLog
from app.services.account_validation import (
    AccountValidationError,
    ValidationCode,
    ValidationStage,
    _save_tokens_and_measure,
)


_DEVICE_SESSION_TTL = timedelta(minutes=15)


@dataclass(slots=True)
class DeviceAuthSession:
    id: str
    account_id: int
    job_id: int
    code: DeviceCode | None
    created_at: datetime
    expires_at: datetime
    next_poll_at: datetime
    status: str = "pending"
    error_code: str | None = None
    error_detail: str | None = None


class AccountDeviceAuthManager:
    """Short-lived, single-process manager for operator-assisted OpenAI login.

    The server never sees the account password during this flow. The operator
    completes OpenAI's own device page, while only the resulting OAuth tokens
    are encrypted in the database.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, DeviceAuthSession] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        session: AsyncSession,
        account: Account,
    ) -> DeviceAuthSession:
        code = await request_device_code()
        now = datetime.now(timezone.utc)
        async with self._lock:
            for existing in self._sessions.values():
                if existing.account_id == account.id and existing.status == "pending":
                    existing.status = "expired"
                    existing.code = None

            job = AccountCheckJob(
                account_id=account.id,
                priority="manual",
                job_type="device_auth",
                status="pending",
            )
            session.add(job)
            await session.flush()
            account.status = "pending_validation"
            session.add(AuditLog(
                event_type="account_device_auth_started",
                account_id=account.id,
                metadata_={"actor": "admin", "job_id": job.id},
            ))
            await session.commit()

            auth_session = DeviceAuthSession(
                id=uuid.uuid4().hex,
                account_id=account.id,
                job_id=job.id,
                code=code,
                created_at=now,
                expires_at=now + _DEVICE_SESSION_TTL,
                next_poll_at=now,
            )
            self._sessions[auth_session.id] = auth_session
            return auth_session

    async def poll(
        self,
        db: AsyncSession,
        account: Account,
        session_id: str,
    ) -> DeviceAuthSession:
        async with self._lock:
            auth_session = self._sessions.get(session_id)
            if auth_session is None or auth_session.account_id != account.id:
                raise KeyError(session_id)
            if auth_session.status != "pending":
                return auth_session

            now = datetime.now(timezone.utc)
            if now >= auth_session.expires_at:
                await self._fail(
                    db,
                    account,
                    auth_session,
                    "device_auth_expired",
                    "Время подтверждения входа истекло.",
                )
                auth_session.status = "expired"
                return auth_session
            if now < auth_session.next_poll_at:
                return auth_session

            code = auth_session.code
            if code is None:
                await self._fail(
                    db,
                    account,
                    auth_session,
                    "device_auth_state_lost",
                    "Состояние подтверждения входа потеряно.",
                )
                return auth_session
            auth_session.next_poll_at = now + timedelta(seconds=code.interval_seconds)

            job = await db.get(AccountCheckJob, auth_session.job_id)
            if job is not None and job.status == "pending":
                job.status = "running"
                job.started_at = now
                await db.commit()

            try:
                authorization = await poll_device_authorization(
                    code.device_auth_id,
                    code.user_code,
                )
            except DeviceAuthError:
                await self._fail(
                    db,
                    account,
                    auth_session,
                    "device_auth_poll_failed",
                    "OpenAI отклонил проверку кода устройства.",
                )
                return auth_session
            if authorization is None:
                return auth_session

            try:
                tokens = await exchange_device_authorization(authorization)
                claims = parse_id_token(tokens.id_token) if tokens.id_token else None
                if claims is None or not self._identity_matches(account, claims.email):
                    raise AccountValidationError(
                        ValidationStage.LOGIN,
                        ValidationCode.INVALID_CREDENTIALS,
                        "В браузере подтверждён другой аккаунт OpenAI.",
                    )
                await _save_tokens_and_measure(db, account, tokens)
            except AccountValidationError as exc:
                await self._fail(
                    db,
                    account,
                    auth_session,
                    exc.code,
                    exc.detail,
                    stage=exc.stage,
                )
                return auth_session
            except Exception:
                await self._fail(
                    db,
                    account,
                    auth_session,
                    "device_auth_exchange_failed",
                    "Не удалось завершить обмен токенов OpenAI.",
                )
                return auth_session

            finished_at = datetime.now(timezone.utc)
            job = await db.get(AccountCheckJob, auth_session.job_id)
            if job is not None:
                job.status = "done"
                job.result = "ok"
                job.finished_at = finished_at
            db.add(AuditLog(
                event_type="account_device_auth_completed",
                account_id=account.id,
                metadata_={"actor": "admin", "job_id": auth_session.job_id},
            ))
            await db.commit()
            auth_session.status = "completed"
            auth_session.code = None
            return auth_session

    @staticmethod
    def _identity_matches(account: Account, token_email: str | None) -> bool:
        if not token_email:
            return False
        expected = {
            value.strip().casefold()
            for value in (account.login, account.email)
            if value and "@" in value
        }
        return token_email.strip().casefold() in expected

    async def _fail(
        self,
        db: AsyncSession,
        account: Account,
        auth_session: DeviceAuthSession,
        code: str,
        detail: str,
        *,
        stage: str = "device_auth",
    ) -> None:
        now = datetime.now(timezone.utc)
        account.status = "validation_failed"
        job = await db.get(AccountCheckJob, auth_session.job_id)
        if job is not None:
            job.status = "failed"
            job.finished_at = now
            job.error = json.dumps(
                {"stage": stage, "code": code, "detail": detail},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        db.add(AuditLog(
            event_type="account_device_auth_failed",
            account_id=account.id,
            metadata_={"actor": "admin", "job_id": auth_session.job_id, "code": code},
        ))
        await db.commit()
        auth_session.status = "failed"
        auth_session.error_code = code
        auth_session.error_detail = detail
        auth_session.code = None


account_device_auth_manager = AccountDeviceAuthManager()
