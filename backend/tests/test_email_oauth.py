import base64
import hashlib
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.config import get_settings
from app.main import app
from app.models.account import Account, AccountCheckJob, EmailOAuthCredential
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.rental import Order, Rental
from app.services.email_oauth import (
    EmailOAuthStateError,
    EmailOAuthStateManager,
    MicrosoftGraphOAuthConfig,
)


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(COOKIE_NAME, create_access_token())
        yield client


def _configure_graph(monkeypatch) -> None:
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_ID", "graph-client-id")
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_SECRET", "graph-client-secret")
    monkeypatch.setenv(
        "MICROSOFT_GRAPH_REDIRECT_URI",
        "https://test/api/email-oauth/microsoft/callback",
    )
    get_settings.cache_clear()


async def _outlook_account(session: AsyncSession, email: str) -> Account:
    account = Account(
        login=f"openai-{email}",
        password_encrypted="openai-password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        email=email,
        email_password_encrypted="mail-password",
        status="pending_validation",
    )
    session.add(account)
    await session.commit()
    return account


async def _occupy_account(session: AsyncSession, account: Account) -> Rental:
    tier = SubscriptionTier(code="plus", name="Plus", is_active=True)
    duration = Duration(minutes=60, is_enabled=True, sort_order=60)
    scope = LimitScope(code="any", name="Any")
    session.add_all([tier, duration, scope])
    await session.flush()
    account.tier_id = tier.id
    account.status = "active"
    order = Order(
        funpay_order_id=f"oauth-order-{account.id}",
        funpay_chat_id=f"oauth-chat-{account.id}",
        buyer_funpay_id=f"oauth-buyer-{account.id}",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        status="completed",
    )
    session.add(order)
    await session.flush()
    rental = Rental(
        order_id=order.id,
        account_id=account.id,
        buyer_funpay_id=order.buyer_funpay_id,
        buyer_funpay_chat_id=order.funpay_chat_id,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        status="active",
        credentials_delivery_status="sent",
        credentials_delivery_template="welcome",
        credentials_delivery_attempts=1,
    )
    session.add(rental)
    await session.commit()
    return rental


async def test_state_is_pkce_bound_one_time_and_expires():
    manager = EmailOAuthStateManager()
    config = MicrosoftGraphOAuthConfig(
        "client-id", "client-secret", "https://example.test/oauth/callback"
    )
    start = await manager.start(
        account_id=17,
        expected_email="Owner@Outlook.com",
        config=config,
    )
    params = parse_qs(urlparse(start.authorization_url).query)
    state = params["state"][0]
    pending = await manager.consume(state)

    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(pending.code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [expected_challenge]
    assert pending.account_id == 17
    assert pending.expected_email == "owner@outlook.com"
    with pytest.raises(EmailOAuthStateError):
        await manager.consume(state)

    expired_manager = EmailOAuthStateManager(ttl=timedelta(seconds=-1))
    expired = await expired_manager.start(
        account_id=17,
        expected_email="owner@outlook.com",
        config=config,
    )
    expired_state = parse_qs(urlparse(expired.authorization_url).query)["state"][0]
    with pytest.raises(EmailOAuthStateError):
        await expired_manager.consume(expired_state)


async def test_start_returns_clear_503_without_client_configuration(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    account = await _outlook_account(session, "owner@outlook.com")

    response = await auth_client.post(
        f"/api/accounts/{account.id}/email-oauth/microsoft"
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Microsoft Graph OAuth is not configured"
    }


async def test_start_rejects_non_outlook_address(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    _configure_graph(monkeypatch)
    account = await _outlook_account(session, "owner@gmail.com")

    response = await auth_client.post(
        f"/api/accounts/{account.id}/email-oauth/microsoft"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Account email must be a personal Outlook/Hotmail address"
    )


async def test_start_rejects_mailbox_oauth_for_occupied_account(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    _configure_graph(monkeypatch)
    account = await _outlook_account(session, "occupied@outlook.com")
    await _occupy_account(session, account)

    response = await auth_client.post(
        f"/api/accounts/{account.id}/email-oauth/microsoft"
    )

    assert response.status_code == 409
    await session.refresh(account)
    assert account.status == "active"


async def test_callback_verifies_identity_encrypts_refresh_and_exposes_status(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    httpx_mock,
):
    _configure_graph(monkeypatch)
    account = await _outlook_account(session, "owner@outlook.com")
    started = await auth_client.post(
        f"/api/accounts/{account.id}/email-oauth/microsoft"
    )
    assert started.status_code == 200
    assert started.headers["cache-control"] == "no-store"
    authorization = started.json()["authorization_url"]
    params = parse_qs(urlparse(authorization).query)
    state = params["state"][0]
    assert urlparse(authorization).path.endswith("/consumers/oauth2/v2.0/authorize")
    assert "https://graph.microsoft.com/Mail.Read" in params["scope"][0]
    assert params["response_mode"] == ["form_post"]

    httpx_mock.add_response(
        method="POST",
        url="https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        json={
            "access_token": "graph-access-token",
            "refresh_token": "graph-refresh-token",
            "scope": "openid offline_access User.Read Mail.Read",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"https://graph\.microsoft\.com/v1\.0/me.*"),
        json={
            "id": "microsoft-subject",
            "mail": "OWNER@outlook.com",
            "userPrincipalName": "owner@outlook.com",
        },
    )

    transport = ASGITransport(app=app)
    lifecycle = MagicMock()
    app.state.lifecycle = lifecycle
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as public:
            callback = await public.post(
                "/api/email-oauth/microsoft/callback",
                data={"state": state, "code": "authorization-code"},
            )
    finally:
        del app.state.lifecycle

    assert callback.status_code == 303
    assert callback.headers["location"] == "/accounts?email_oauth=connected"
    credential = await session.get(EmailOAuthCredential, account.id)
    assert credential is not None
    assert credential.refresh_token_encrypted == "graph-refresh-token"
    raw_token = (
        await session.execute(
            text(
                "SELECT refresh_token_encrypted FROM email_oauth_credentials "
                "WHERE account_id=:account_id"
            ),
            {"account_id": account.id},
        )
    ).scalar_one()
    assert raw_token != "graph-refresh-token"
    assert "graph-refresh-token" not in raw_token
    await session.refresh(account)
    assert account.status == "pending_validation"
    job = (
        await session.execute(
            select(AccountCheckJob).where(
                AccountCheckJob.account_id == account.id,
                AccountCheckJob.status == "pending",
            )
        )
    ).scalar_one()
    assert job.job_type == "full_validation"
    assert job.priority == "manual"
    lifecycle.request_capacity_reconcile.assert_called_once_with()

    account_response = await auth_client.get(f"/api/accounts/{account.id}")
    payload = account_response.json()
    assert payload["email_oauth_connected"] is True
    assert payload["email_oauth_provider"] == "microsoft_graph"
    assert payload["email_oauth_status"] == "connected"
    assert "graph-refresh-token" not in account_response.text

    token_request = httpx_mock.get_requests()[0]
    token_form = parse_qs(token_request.content.decode())
    assert token_form["client_secret"] == ["graph-client-secret"]
    assert token_form["code_verifier"][0]
    assert token_form["code"] == ["authorization-code"]


async def test_callback_rechecks_occupancy_before_storing_mailbox_token(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    httpx_mock,
):
    _configure_graph(monkeypatch)
    account = await _outlook_account(session, "raced@outlook.com")
    started = await auth_client.post(
        f"/api/accounts/{account.id}/email-oauth/microsoft"
    )
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)[
        "state"
    ][0]
    await _occupy_account(session, account)
    httpx_mock.add_response(
        method="POST",
        url="https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        json={
            "access_token": "occupied-access-token",
            "refresh_token": "occupied-refresh-token",
            "scope": "openid offline_access User.Read Mail.Read",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"https://graph\.microsoft\.com/v1\.0/me.*"),
        json={
            "id": "occupied-subject",
            "mail": "raced@outlook.com",
            "userPrincipalName": "raced@outlook.com",
        },
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as public:
        callback = await public.post(
            "/api/email-oauth/microsoft/callback",
            data={"state": state, "code": "occupied-code"},
        )

    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "/accounts?email_oauth=failed&reason=account_in_use"
    )
    assert await session.get(EmailOAuthCredential, account.id) is None
    await session.refresh(account)
    assert account.status == "active"

    async with AsyncClient(transport=transport, base_url="http://test") as public:
        replay = await public.post(
            "/api/email-oauth/microsoft/callback",
            data={"state": state, "code": "authorization-code"},
        )
    assert replay.status_code == 303
    assert replay.headers["location"] == (
        "/accounts?email_oauth=failed&reason=invalid_state"
    )


async def test_callback_email_mismatch_never_stores_token_or_leaks_it(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    httpx_mock,
):
    _configure_graph(monkeypatch)
    account = await _outlook_account(session, "expected@hotmail.com")
    started = await auth_client.post(
        f"/api/accounts/{account.id}/email-oauth/microsoft"
    )
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)[
        "state"
    ][0]
    httpx_mock.add_response(
        method="POST",
        url="https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        json={
            "access_token": "mismatch-access-token",
            "refresh_token": "mismatch-refresh-token",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"https://graph\.microsoft\.com/v1\.0/me.*"),
        json={
            "id": "other-subject",
            "mail": "other@outlook.com",
            "userPrincipalName": "other@outlook.com",
        },
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as public:
        callback = await public.post(
            "/api/email-oauth/microsoft/callback",
            data={"state": state, "code": "mismatch-code"},
        )

    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "/accounts?email_oauth=failed&reason=email_mismatch"
    )
    assert "mismatch" not in callback.text
    assert await session.get(EmailOAuthCredential, account.id) is None


async def test_callback_rejects_oversized_body_without_valid_state():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as public:
        response = await public.post(
            "/api/email-oauth/microsoft/callback",
            content=b"state=" + b"x" * 20_000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/accounts?email_oauth=failed&reason=invalid_state"
    )
