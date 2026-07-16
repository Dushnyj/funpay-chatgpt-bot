import pytest

from app.integrations.openai.device_auth import (
    DEVICE_AUTH_REDIRECT_URI,
    DeviceAuthorization,
    DeviceAuthError,
    exchange_device_authorization,
    poll_device_authorization,
    request_device_code,
)
from app.integrations.openai.oauth import RefreshedTokens
from app.integrations.playwright.proxy import BrowserProxy


@pytest.mark.asyncio
async def test_request_device_code_accepts_string_interval(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://auth.openai.com/api/accounts/deviceauth/usercode",
        json={
            "device_auth_id": "device-id",
            "user_code": "ABCD-EFGH",
            "interval": "3",
        },
    )
    result = await request_device_code()
    assert result.device_auth_id == "device-id"
    assert result.user_code == "ABCD-EFGH"
    assert result.interval_seconds == 3
    assert result.verification_url == "https://auth.openai.com/codex/device"


@pytest.mark.asyncio
async def test_poll_device_authorization_pending(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://auth.openai.com/api/accounts/deviceauth/token",
        status_code=403,
    )
    assert await poll_device_authorization("device-id", "ABCD-EFGH") is None


@pytest.mark.asyncio
async def test_poll_and_exchange_device_authorization(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://auth.openai.com/api/accounts/deviceauth/token",
        json={
            "authorization_code": "authorization-code",
            "code_verifier": "verifier",
            "code_challenge": "challenge",
        },
    )
    httpx_mock.add_response(
        method="POST",
        url="https://auth.openai.com/oauth/token",
        json={
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id-token",
        },
    )
    authorization = await poll_device_authorization("device-id", "ABCD-EFGH")
    assert authorization is not None
    tokens = await exchange_device_authorization(authorization)
    assert tokens.refresh_token == "refresh"
    request = httpx_mock.get_requests()[-1]
    assert f"redirect_uri={DEVICE_AUTH_REDIRECT_URI}" in request.content.decode()


@pytest.mark.asyncio
async def test_device_auth_errors_are_safe_codes(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://auth.openai.com/api/accounts/deviceauth/usercode",
        status_code=500,
        text="secret upstream response",
    )
    with pytest.raises(DeviceAuthError, match="device_code_request_failed:500"):
        await request_device_code()


@pytest.mark.asyncio
async def test_device_auth_http_steps_keep_selected_proxy(monkeypatch):
    selected = BrowserProxy(21, "http", "proxy.internal", 3128)
    observed: list[BrowserProxy | None] = []
    responses = [
        {
            "device_auth_id": "device-id",
            "user_code": "ABCD-EFGH",
            "interval": "1",
        },
        {
            "authorization_code": "authorization-code",
            "code_verifier": "verifier",
            "code_challenge": "challenge",
        },
    ]

    class FakeResponse:
        is_success = True
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse(responses.pop(0))

    def fake_client(*, proxy=None, timeout=30.0):
        observed.append(proxy)
        return FakeClient()

    exchanged: list[BrowserProxy | None] = []

    async def fake_exchange(*_args, proxy=None):
        exchanged.append(proxy)
        return RefreshedTokens("access", "refresh", None)

    monkeypatch.setattr(
        "app.integrations.openai.device_auth.openai_http_client", fake_client
    )
    monkeypatch.setattr(
        "app.integrations.openai.device_auth.exchange_code_for_tokens", fake_exchange
    )

    device_code = await request_device_code(proxy=selected)
    authorization = await poll_device_authorization(
        device_code.device_auth_id,
        device_code.user_code,
        proxy=selected,
    )
    assert authorization == DeviceAuthorization(
        "authorization-code", "verifier", "challenge"
    )
    await exchange_device_authorization(authorization, proxy=selected)

    assert observed == [selected, selected]
    assert exchanged == [selected]
