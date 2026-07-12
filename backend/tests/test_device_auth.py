import pytest

from app.integrations.openai.device_auth import (
    DEVICE_AUTH_REDIRECT_URI,
    DeviceAuthError,
    exchange_device_authorization,
    poll_device_authorization,
    request_device_code,
)


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
