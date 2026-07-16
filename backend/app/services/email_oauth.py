from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import httpx

from app.config import Settings
from app.integrations.email.microsoft_graph_provider import (
    MICROSOFT_AUTHORITY,
    MICROSOFT_GRAPH_SCOPE_STRING,
    microsoft_graph_http_client,
)
from app.integrations.playwright.proxy import BrowserProxy, ProxyUnavailableError


_STATE_TTL = timedelta(minutes=10)
_PROFILE_URL = "https://graph.microsoft.com/v1.0/me"


class EmailOAuthConfigurationError(RuntimeError):
    pass


class EmailOAuthStateError(RuntimeError):
    pass


class EmailOAuthExchangeError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class MicrosoftGraphOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_settings(cls, settings: Settings) -> MicrosoftGraphOAuthConfig:
        client_id = settings.microsoft_graph_client_id.strip()
        client_secret = settings.microsoft_graph_client_secret.strip()
        redirect_uri = settings.microsoft_graph_redirect_uri.strip()
        if not client_id or not client_secret or not redirect_uri:
            raise EmailOAuthConfigurationError(
                "Microsoft Graph OAuth is not configured"
            )

        parsed = urlparse(redirect_uri)
        localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.netloc
            or parsed.fragment
            or parsed.scheme not in {"http", "https"}
            or (parsed.scheme != "https" and not localhost)
        ):
            raise EmailOAuthConfigurationError(
                "Microsoft Graph redirect URI must be HTTPS"
            )
        return cls(client_id, client_secret, redirect_uri)


@dataclass(frozen=True, slots=True)
class PendingEmailOAuth:
    account_id: int
    expected_email: str
    code_verifier: str
    client_id: str
    redirect_uri: str
    expires_at: datetime
    browser_proxy: BrowserProxy | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class EmailOAuthStart:
    authorization_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedMicrosoftTokens:
    refresh_token: str
    scopes: str
    external_subject: str | None


class EmailOAuthStateManager:
    """Short-lived, one-time PKCE state storage for the admin OAuth flow."""

    def __init__(self, *, ttl: timedelta = _STATE_TTL) -> None:
        self._ttl = ttl
        self._pending: dict[str, PendingEmailOAuth] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        account_id: int,
        expected_email: str,
        config: MicrosoftGraphOAuthConfig,
        browser_proxy: BrowserProxy | None = None,
    ) -> EmailOAuthStart:
        now = datetime.now(timezone.utc)
        expires_at = now + self._ttl
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        pending = PendingEmailOAuth(
            account_id=account_id,
            expected_email=expected_email.strip().casefold(),
            code_verifier=code_verifier,
            client_id=config.client_id,
            redirect_uri=config.redirect_uri,
            expires_at=expires_at,
            browser_proxy=browser_proxy,
        )

        async with self._lock:
            self._cleanup(now)
            self._pending[state] = pending

        query = urlencode(
            {
                "client_id": config.client_id,
                "response_type": "code",
                "redirect_uri": config.redirect_uri,
                # Keep the short-lived authorization code out of web-server
                # access-log URLs. The callback accepts this URL-encoded POST.
                "response_mode": "form_post",
                "scope": MICROSOFT_GRAPH_SCOPE_STRING,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
                "login_hint": expected_email,
            }
        )
        return EmailOAuthStart(
            authorization_url=f"{MICROSOFT_AUTHORITY}/authorize?{query}",
            expires_at=expires_at,
        )

    async def consume(self, state: str) -> PendingEmailOAuth:
        if not state:
            raise EmailOAuthStateError("invalid_state")
        now = datetime.now(timezone.utc)
        async with self._lock:
            self._cleanup(now)
            pending = self._pending.pop(state, None)
        if pending is None or pending.expires_at <= now:
            raise EmailOAuthStateError("invalid_state")
        return pending

    def _cleanup(self, now: datetime) -> None:
        expired = [
            state
            for state, pending in self._pending.items()
            if pending.expires_at <= now
        ]
        for state in expired:
            self._pending.pop(state, None)


async def exchange_and_verify_microsoft_code(
    *,
    code: str,
    pending: PendingEmailOAuth,
    config: MicrosoftGraphOAuthConfig,
    request_timeout_s: float = 15.0,
) -> VerifiedMicrosoftTokens:
    if (
        config.client_id != pending.client_id
        or config.redirect_uri != pending.redirect_uri
    ):
        raise EmailOAuthExchangeError("configuration_changed")
    # The route is part of the one-time state. Callers cannot accidentally
    # exchange a proxy-bound code through a direct client.
    browser_proxy = pending.browser_proxy

    try:
        async with microsoft_graph_http_client(
            browser_proxy=browser_proxy,
            timeout=request_timeout_s,
        ) as client:
            token_response = await client.post(
                f"{MICROSOFT_AUTHORITY}/token",
                data={
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": config.redirect_uri,
                    "code_verifier": pending.code_verifier,
                    "scope": MICROSOFT_GRAPH_SCOPE_STRING,
                },
            )
    except httpx.HTTPError as exc:
        if browser_proxy is not None:
            raise ProxyUnavailableError(
                "Маршрут входа в почту через прокси недоступен."
            ) from None
        raise EmailOAuthExchangeError("token_service_unavailable") from exc

    if token_response.is_error:
        raise EmailOAuthExchangeError("token_exchange_failed")
    token_payload = _response_object(token_response)
    access_token = token_payload.get("access_token")
    refresh_token = token_payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise EmailOAuthExchangeError("token_exchange_failed")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise EmailOAuthExchangeError("offline_access_missing")

    try:
        async with microsoft_graph_http_client(
            browser_proxy=browser_proxy,
            timeout=request_timeout_s,
        ) as client:
            profile_response = await client.get(
                _PROFILE_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                params={"$select": "id,mail,userPrincipalName"},
            )
    except httpx.HTTPError as exc:
        if browser_proxy is not None:
            raise ProxyUnavailableError(
                "Маршрут входа в почту через прокси недоступен."
            ) from None
        raise EmailOAuthExchangeError("profile_service_unavailable") from exc
    if profile_response.is_error:
        raise EmailOAuthExchangeError("profile_lookup_failed")

    profile = _response_object(profile_response)
    addresses = {
        value.strip().casefold()
        for field in ("mail", "userPrincipalName")
        if isinstance((value := profile.get(field)), str) and value.strip()
    }
    if pending.expected_email not in addresses:
        raise EmailOAuthExchangeError("email_mismatch")

    subject = profile.get("id")
    scopes = token_payload.get("scope")
    return VerifiedMicrosoftTokens(
        refresh_token=refresh_token,
        scopes=scopes if isinstance(scopes, str) else MICROSOFT_GRAPH_SCOPE_STRING,
        external_subject=subject if isinstance(subject, str) and subject else None,
    )


def _response_object(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise EmailOAuthExchangeError("invalid_response") from exc
    if not isinstance(payload, dict):
        raise EmailOAuthExchangeError("invalid_response")
    return payload


email_oauth_state_manager = EmailOAuthStateManager()
