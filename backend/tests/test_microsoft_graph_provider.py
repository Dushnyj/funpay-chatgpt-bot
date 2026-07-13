import re
from unittest.mock import AsyncMock

import pytest

from app.integrations.email.microsoft_graph_provider import (
    MICROSOFT_AUTHORITY,
    MicrosoftGraphEmailProvider,
)
from app.integrations.email.provider import EmailErrorCode, EmailProviderError


def _provider(on_refresh_token=None) -> MicrosoftGraphEmailProvider:
    return MicrosoftGraphEmailProvider(
        "owner@outlook.com",
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="old-refresh-token",
        on_refresh_token=on_refresh_token or AsyncMock(),
        poll_interval_s=0,
    )


async def test_refresh_token_rotation_is_persisted_before_graph_read(httpx_mock):
    persist = AsyncMock()
    provider = _provider(persist)
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={
            "access_token": "access-token",
            "refresh_token": "rotated-refresh-token",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/inbox/messages.*"
        ),
        json={"value": []},
    )

    await provider.preflight()

    persist.assert_awaited_once_with("rotated-refresh-token")
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    assert b"old-refresh-token" in requests[0].content
    assert requests[1].headers["Authorization"] == "Bearer access-token"


async def test_preflight_baseline_excludes_old_code_and_accepts_new_message(
    httpx_mock,
):
    provider = _provider()
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={"access_token": "access-token"},
    )
    messages_url = re.compile(
        r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/inbox/messages.*"
    )
    old = {
        "id": "old",
        "subject": "Your code is 111111",
        "bodyPreview": "111111",
        "body": {"content": "111111"},
        "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
    }
    new = {
        "id": "new",
        "subject": "Your ChatGPT verification code",
        "bodyPreview": "Code 654321",
        "body": {"content": "Use 654321 to continue"},
        "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
    }
    httpx_mock.add_response(method="GET", url=messages_url, json={"value": [old]})
    httpx_mock.add_response(
        method="GET", url=messages_url, json={"value": [new, old]}
    )

    await provider.preflight()
    code = await provider.fetch_verification_code(timeout=1)

    assert code == "654321"


async def test_graph_provider_does_not_accept_spoofed_sender(httpx_mock):
    provider = _provider()
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={"access_token": "access-token"},
    )
    messages_url = re.compile(
        r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/inbox/messages.*"
    )
    httpx_mock.add_response(method="GET", url=messages_url, json={"value": []})
    httpx_mock.add_response(
        method="GET",
        url=messages_url,
        json={
            "value": [
                {
                    "id": "spoof",
                    "subject": "OpenAI verification code 123456",
                    "bodyPreview": "123456",
                    "body": {"content": "123456"},
                    "from": {
                        "emailAddress": {
                            "address": "noreply@openai.com.attacker.test"
                        }
                    },
                }
            ]
        },
    )

    await provider.preflight()
    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_verification_code(timeout=0)

    assert error.value.code is EmailErrorCode.NO_CODE


async def test_token_error_is_safe(httpx_mock):
    reauthorize = AsyncMock()
    provider = MicrosoftGraphEmailProvider(
        "owner@outlook.com",
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="old-refresh-token",
        on_refresh_token=AsyncMock(),
        on_reauthorization_required=reauthorize,
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        status_code=400,
        json={
            "error": "invalid_grant",
            "error_description": "leaked-refresh-token-value",
        },
    )

    with pytest.raises(EmailProviderError) as error:
        await provider.preflight()

    assert error.value.code is EmailErrorCode.AUTH_FAILED
    reauthorize.assert_awaited_once_with()
    assert "leaked-refresh-token-value" not in str(error.value)
    assert "old-refresh-token" not in str(error.value)
