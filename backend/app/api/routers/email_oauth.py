from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.check_job_queue import ActiveJobConflict, CheckJobQueue
from app.config import get_settings
from app.integrations.email.outlook_web_provider import is_outlook_address
from app.models.account import Account, EmailOAuthCredential
from app.models.audit import AuditLog
from app.services.email_oauth import (
    EmailOAuthConfigurationError,
    EmailOAuthExchangeError,
    EmailOAuthStateError,
    MicrosoftGraphOAuthConfig,
    email_oauth_state_manager,
    exchange_and_verify_microsoft_code,
)


router = APIRouter(tags=["email-oauth"])
_check_job_queue = CheckJobQueue()
_CALLBACK_BODY_LIMIT = 16_384


class EmailOAuthStartOut(BaseModel):
    authorization_url: str
    expires_at: datetime


def _accounts_redirect(status: str, reason: str | None = None) -> RedirectResponse:
    query = {"email_oauth": status}
    if reason is not None:
        query["reason"] = reason
    response = RedirectResponse(
        url=f"/accounts?{urlencode(query)}",
        status_code=303,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post(
    "/api/accounts/{account_id}/email-oauth/microsoft",
    response_model=EmailOAuthStartOut,
    dependencies=[Depends(get_current_user)],
)
async def start_microsoft_email_oauth(
    account_id: int,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.email or not is_outlook_address(account.email):
        raise HTTPException(
            status_code=400,
            detail="Account email must be a personal Outlook/Hotmail address",
        )
    try:
        config = MicrosoftGraphOAuthConfig.from_settings(get_settings())
    except EmailOAuthConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail="Microsoft Graph OAuth is not configured",
        ) from exc

    pending = await email_oauth_state_manager.start(
        account_id=account.id,
        expected_email=account.email,
        config=config,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return EmailOAuthStartOut(
        authorization_url=pending.authorization_url,
        expires_at=pending.expires_at,
    )


@router.post("/api/email-oauth/microsoft/callback")
async def microsoft_email_oauth_callback(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    body = await _read_bounded_body(request, _CALLBACK_BODY_LIMIT)
    if content_type != "application/x-www-form-urlencoded" or body is None:
        return _accounts_redirect("failed", "invalid_state")
    try:
        form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return _accounts_redirect("failed", "invalid_state")
    state = form.get("state", [""])[0]
    code = form.get("code", [None])[0]
    error = form.get("error", [None])[0]
    if (
        not isinstance(state, str)
        or len(state) > 256
        or (isinstance(code, str) and len(code) > 8192)
        or (isinstance(error, str) and len(error) > 256)
    ):
        return _accounts_redirect("failed", "invalid_state")
    try:
        pending = await email_oauth_state_manager.consume(state)
    except EmailOAuthStateError:
        return _accounts_redirect("failed", "invalid_state")

    if error is not None:
        reason = "access_denied" if error == "access_denied" else "provider_error"
        return _accounts_redirect("failed", reason)
    if not code:
        return _accounts_redirect("failed", "missing_code")

    account = await session.get(Account, pending.account_id)
    if (
        account is None
        or not account.email
        or account.email.strip().casefold() != pending.expected_email
    ):
        return _accounts_redirect("failed", "account_changed")

    try:
        config = MicrosoftGraphOAuthConfig.from_settings(get_settings())
        tokens = await exchange_and_verify_microsoft_code(
            code=code,
            pending=pending,
            config=config,
        )
    except EmailOAuthConfigurationError:
        return _accounts_redirect("failed", "configuration_missing")
    except EmailOAuthExchangeError as exc:
        return _accounts_redirect("failed", exc.reason)

    now = datetime.now(timezone.utc)
    credential = await session.get(EmailOAuthCredential, account.id)
    if credential is None:
        credential = EmailOAuthCredential(
            account_id=account.id,
            provider="microsoft_graph",
            email=account.email,
            refresh_token_encrypted=tokens.refresh_token,
            scopes=tokens.scopes,
            status="connected",
            connected_at=now,
            updated_at=now,
        )
        session.add(credential)
    else:
        credential.provider = "microsoft_graph"
        credential.email = account.email
        credential.refresh_token_encrypted = tokens.refresh_token
        credential.scopes = tokens.scopes
        credential.status = "connected"
        credential.connected_at = now
        credential.updated_at = now
    credential.external_subject = tokens.external_subject
    try:
        job = await _check_job_queue.enqueue_exclusive(
            session,
            account.id,
            priority="manual",
            job_type="full_validation",
            superseded_by="email_oauth_connected",
        )
        job_id = job.id
        rerun_requested = False
    except ActiveJobConflict as exc:
        # A published worker lease is already validating this account. Keep
        # the new credential, but never start a competing browser flow. The
        # current worker will consume this durable follow-up flag after it
        # terminalizes its lease.
        job_id = exc.job_id
        account.validation_rerun_requested = True
        rerun_requested = True
    account.status = "pending_validation"
    session.add(
        AuditLog(
            event_type="account_email_oauth_connected",
            account_id=account.id,
            metadata_={
                "actor": "admin",
                "provider": "microsoft_graph",
                "job_id": job_id,
                "rerun_requested": rerun_requested,
            },
        )
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        return _accounts_redirect("failed", "storage_failed")
    return _accounts_redirect("connected")


async def _read_bounded_body(request: Request, limit: int) -> bytes | None:
    """Read a small form body without first buffering an unbounded request."""

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                return None
        except ValueError:
            return None

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
