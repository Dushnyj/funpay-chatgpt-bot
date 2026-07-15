from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import async_session_factory
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
from app.check_job_queue import CheckJobQueue
from app.services.account_validation import (
    AccountValidationError,
    ValidationCode,
    ValidationStage,
    _save_tokens_and_measure,
)
from app.services.account_occupancy import account_is_busy


_DEVICE_SESSION_TTL = timedelta(minutes=15)
_BACKGROUND_RETRY_DELAY_SECONDS = 1.0

logger = logging.getLogger(__name__)


class AccountBusyError(RuntimeError):
    """A buyer owns, or a replacement claim reserves, this account."""


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

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        validation_queued: Callable[[], None] | None = None,
    ) -> None:
        self._sessions: dict[str, DeviceAuthSession] = {}
        self._lock = asyncio.Lock()
        self._queue = CheckJobQueue()
        self._session_factory = session_factory
        self._validation_queued = validation_queued
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def set_validation_queued_callback(
        self,
        callback: Callable[[], None] | None,
    ) -> None:
        """Attach the live scheduler without coupling this service to FastAPI."""

        self._validation_queued = callback

    def _notify_validation_queued(self) -> None:
        callback = self._validation_queued
        if callback is None:
            return
        try:
            callback()
        except Exception:
            # The job is already committed; the periodic worker remains the
            # recovery path and a wake failure must not corrupt Device Auth.
            logger.exception("Could not wake validation queue after Device Auth")

    async def start(
        self,
        session: AsyncSession,
        account: Account,
    ) -> DeviceAuthSession:
        account_id = account.id
        # Device-code creation is remote I/O and can be slow. Release the
        # preflight transaction, then serialize the state transition again.
        if session.in_transaction():
            await session.commit()
        code = await request_device_code()
        now = datetime.now(timezone.utc)
        # poll() already serializes Device Auth under the process lock before
        # it mutates the database. Keep the same lock order here so a retry
        # cannot hold the account row while a background poll waits to commit.
        async with self._lock:
            account = (
                await session.execute(
                    select(Account)
                    .where(Account.id == account_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if account is None:
                await session.rollback()
                raise KeyError(account_id)
            if await account_is_busy(session, account_id):
                await session.rollback()
                raise AccountBusyError(account_id)

            # A second operator click means "issue a fresh browser code". A
            # running Device Auth job is owned by this manager, so it can be
            # superseded safely under the same lock. Other running validation
            # types remain protected by enqueue_exclusive() and still return a
            # conflict instead of being raced.
            await self._supersede_pending_sessions(session, account.id, now)
            job = await self._queue.enqueue_exclusive(
                session,
                account.id,
                priority="manual",
                job_type="device_auth",
                superseded_by="device_auth",
            )
            account.operator_status_override = None
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
            self._start_background_task(auth_session)
            return auth_session

    async def _supersede_pending_sessions(
        self,
        db: AsyncSession,
        account_id: int,
        now: datetime,
    ) -> None:
        """Retire manager-owned Device Auth attempts before issuing a new code.

        The manager lock must be held by the caller. Persisting the old job in
        the same account-row transaction keeps the durable queue and the
        in-memory code lifecycle in sync.
        """

        for existing in self._sessions.values():
            if existing.account_id != account_id or existing.status != "pending":
                continue

            job = await db.get(AccountCheckJob, existing.job_id)
            if (
                job is not None
                and job.job_type == "device_auth"
                and job.status in {"pending", "running"}
            ):
                job.status = "done"
                job.result = "superseded:device_auth"
                job.error = None
                job.finished_at = now
                db.add(
                    AuditLog(
                        event_type="account_device_auth_restarted",
                        account_id=account_id,
                        metadata_={"actor": "admin", "job_id": job.id},
                    )
                )

            existing.status = "expired"
            existing.error_code = "device_auth_restarted"
            existing.error_detail = (
                "Предыдущий код отменён: начата новая проверка через браузер."
            )
            existing.code = None
            self._cancel_background_task(existing.id)

        await db.flush()

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
                self._cancel_background_task(session_id)
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
                self._cancel_background_task(session_id)
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
            if job is None or job.status not in {"pending", "running"}:
                # A manual recheck or a newer device-auth session may have
                # durably superseded this job while its browser code was still
                # present in this process. Never let that stale session race
                # the replacement validation.
                auth_session.status = "expired"
                auth_session.code = None
                self._cancel_background_task(session_id)
                return auth_session
            if job.status == "pending":
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
                job.result = "tokens_connected"
                job.finished_at = finished_at
            # Device authorization proves mailbox identity and supplies API
            # tokens, but it never proves the password/TOTP that a buyer will
            # receive. Keep the account unsellable and enqueue the normal
            # credential login validation before it can return to active.
            account.status = account.operator_status_override or "pending_validation"
            account.validation_rerun_requested = False
            await db.flush()
            credential_job = await self._queue.enqueue(
                db,
                account.id,
                priority="manual",
                job_type="full_validation",
            )
            db.add(AuditLog(
                event_type="account_device_auth_completed",
                account_id=account.id,
                metadata_={
                    "actor": "admin",
                    "job_id": auth_session.job_id,
                    "credential_validation_job_id": credential_job.id,
                },
            ))
            await db.commit()
            self._notify_validation_queued()
            auth_session.status = "completed"
            auth_session.code = None
            self._cancel_background_task(session_id)
            return auth_session

    def _start_background_task(self, auth_session: DeviceAuthSession) -> None:
        if self._session_factory is None:
            return
        task = asyncio.create_task(
            self._poll_in_background(auth_session.id),
            name=f"account-device-auth-{auth_session.id[:12]}",
        )
        self._tasks[auth_session.id] = task
        task.add_done_callback(
            lambda completed, session_id=auth_session.id: self._task_done(
                session_id, completed
            )
        )

    async def _poll_in_background(self, session_id: str) -> None:
        assert self._session_factory is not None
        while True:
            async with self._lock:
                auth_session = self._sessions.get(session_id)
                if auth_session is None or auth_session.status != "pending":
                    return
                account_id = auth_session.account_id
                expires_at = auth_session.expires_at

            try:
                async with self._session_factory() as db:
                    account = await db.get(Account, account_id)
                    if account is None:
                        async with self._lock:
                            current = self._sessions.get(session_id)
                            if current is not None and current.status == "pending":
                                current.status = "expired"
                                current.code = None
                        return
                    result = await self.poll(db, account, session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep a transient database failure from making completion
                # depend on a frontend request. No token, code, or body is
                # included in this diagnostic.
                logger.warning(
                    "Background device authorization poll failed for session %s",
                    session_id,
                    exc_info=True,
                )
                result = None

            if result is not None and result.status != "pending":
                return

            now = datetime.now(timezone.utc)
            if now >= expires_at:
                # Let poll() persist the typed expiry on the next iteration.
                delay = 0.05
            elif result is None:
                delay = min(
                    _BACKGROUND_RETRY_DELAY_SECONDS,
                    max((expires_at - now).total_seconds(), 0.05),
                )
            else:
                delay = max(
                    min(
                        (result.next_poll_at - now).total_seconds(),
                        (expires_at - now).total_seconds(),
                    ),
                    0.05,
                )
            await asyncio.sleep(delay)

    def _cancel_background_task(self, session_id: str) -> None:
        task = self._tasks.get(session_id)
        if (
            task is not None
            and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()

    def _task_done(
        self,
        session_id: str,
        completed: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(session_id) is completed:
            self._tasks.pop(session_id, None)
        if completed.cancelled():
            return
        # Retrieve any unexpected exception so asyncio never emits an
        # unhandled-task warning. The loop normally handles and retries it.
        completed.exception()

    async def shutdown(self) -> None:
        """Cancel pollers before the application disposes its DB engine."""

        # Cancel first: a poller can be waiting on the OpenAI network request
        # while holding the manager lock. Cancellation releases that lock and
        # keeps graceful shutdown bounded by local cleanup rather than I/O.
        tasks = list(self._tasks.items())
        for _session_id, task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(
                *(task for _session_id, task in tasks),
                return_exceptions=True,
            )

        async with self._lock:
            interrupted = []
            for auth_session in self._sessions.values():
                if auth_session.status == "pending":
                    interrupted.append(auth_session)
                    auth_session.status = "expired"
                    auth_session.error_code = "device_auth_shutdown"
                    auth_session.error_detail = (
                        "Подтверждение входа прервано перезапуском сервера."
                    )
                    auth_session.code = None
            for session_id, task in tasks:
                if self._tasks.get(session_id) is task:
                    self._tasks.pop(session_id, None)
            self._sessions.clear()
        await self._persist_shutdown_failures(interrupted)

    async def _persist_shutdown_failures(
        self,
        interrupted: list[DeviceAuthSession],
    ) -> None:
        if not interrupted or self._session_factory is None:
            return
        try:
            async with self._session_factory() as db:
                now = datetime.now(timezone.utc)
                for auth_session in interrupted:
                    job = await db.get(AccountCheckJob, auth_session.job_id)
                    if job is not None and job.status in {"pending", "running"}:
                        job.status = "failed"
                        job.finished_at = now
                        job.error = json.dumps(
                            {
                                "stage": "device_auth",
                                "code": "device_auth_shutdown",
                                "detail": auth_session.error_detail,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    account = await db.get(Account, auth_session.account_id)
                    if account is not None:
                        if account.validation_rerun_requested:
                            account.validation_rerun_requested = False
                            account.status = (
                                account.operator_status_override
                                or "pending_validation"
                            )
                            await self._queue.enqueue(
                                db,
                                account.id,
                                priority="manual",
                                job_type="full_validation",
                            )
                        else:
                            account.status = (
                                account.operator_status_override
                                or "validation_failed"
                            )
                await db.commit()
        except Exception:
            logger.warning(
                "Could not persist interrupted device authorization jobs",
                exc_info=True,
            )

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
        await db.refresh(
            account,
            attribute_names=[
                "operator_status_override",
                "validation_rerun_requested",
            ],
        )
        rerun_requested = account.validation_rerun_requested
        account.status = account.operator_status_override or (
            "pending_validation" if rerun_requested else "validation_failed"
        )
        account.validation_rerun_requested = False
        job = await db.get(AccountCheckJob, auth_session.job_id)
        if job is not None:
            job.status = "failed"
            job.finished_at = now
            job.error = json.dumps(
                {"stage": stage, "code": code, "detail": detail},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        await db.flush()
        followup_job = None
        if rerun_requested:
            followup_job = await self._queue.enqueue(
                db,
                account.id,
                priority="manual",
                job_type="full_validation",
            )
        metadata = {
            "actor": "admin",
            "job_id": auth_session.job_id,
            "code": code,
        }
        if followup_job is not None:
            metadata["credential_validation_job_id"] = followup_job.id
        db.add(AuditLog(
            event_type="account_device_auth_failed",
            account_id=account.id,
            metadata_=metadata,
        ))
        await db.commit()
        auth_session.status = "failed"
        auth_session.error_code = code
        auth_session.error_detail = detail
        auth_session.code = None
        self._cancel_background_task(auth_session.id)

account_device_auth_manager = AccountDeviceAuthManager(
    session_factory=async_session_factory
)
