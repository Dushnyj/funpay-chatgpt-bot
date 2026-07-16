import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import app.integrations.email.microsoft_graph_provider as graph_module
from app.integrations.email.microsoft_graph_provider import (
    MICROSOFT_AUTHORITY,
    _MAX_HYDRATED_MESSAGES_PER_LOOKUP,
    MicrosoftGraphEmailProvider,
    microsoft_graph_http_client,
)
from app.integrations.email.provider import EmailErrorCode, EmailProviderError
from app.integrations.playwright.proxy import BrowserProxy, ProxyUnavailableError


_INBOX_MESSAGES_URL = re.compile(
    r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/inbox/messages.*"
)
_JUNK_MESSAGES_URL = re.compile(
    r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/junkemail/messages.*"
)


def _add_mailbox_snapshot(httpx_mock, *, inbox, junk):
    httpx_mock.add_response(
        method="GET",
        url=_INBOX_MESSAGES_URL,
        json={"value": inbox},
    )
    httpx_mock.add_response(
        method="GET",
        url=_JUNK_MESSAGES_URL,
        json={"value": junk},
    )


def _provider(
    on_refresh_token=None,
    *,
    browser_proxy: BrowserProxy | None = None,
) -> MicrosoftGraphEmailProvider:
    return MicrosoftGraphEmailProvider(
        "owner@outlook.com",
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="old-refresh-token",
        on_refresh_token=on_refresh_token or AsyncMock(),
        browser_proxy=browser_proxy,
        poll_interval_s=0,
    )


@pytest.mark.parametrize(
    ("proxy_type", "expected_server"),
    [
        ("http", "http://127.0.0.1:3128"),
        ("https", "http://127.0.0.1:3128"),
        ("socks5", "socks5://127.0.0.1:3128"),
    ],
)
def test_graph_http_client_supports_routes_without_environment_fallback(
    monkeypatch,
    proxy_type,
    expected_server,
):
    captured = {}
    sentinel = object()

    def fake_async_client(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        "app.integrations.email.microsoft_graph_provider.httpx.AsyncClient",
        fake_async_client,
    )
    proxy = BrowserProxy(
        route_id=7,
        proxy_type=proxy_type,
        host="127.0.0.1",
        port=3128,
        username="proxy-user",
        password="proxy-password",
        config_revision=3,
    )

    client = microsoft_graph_http_client(browser_proxy=proxy, timeout=9)

    assert client is sentinel
    assert captured["trust_env"] is False
    assert captured["timeout"] == 9
    assert str(captured["proxy"].url) == expected_server
    assert captured["proxy"].auth == ("proxy-user", "proxy-password")


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
    _add_mailbox_snapshot(httpx_mock, inbox=[], junk=[])

    await provider.preflight()

    persist.assert_awaited_once_with("rotated-refresh-token")
    requests = httpx_mock.get_requests()
    assert len(requests) == 3
    assert b"old-refresh-token" in requests[0].content
    assert requests[1].headers["Authorization"] == "Bearer access-token"
    assert requests[2].headers["Authorization"] == "Bearer access-token"


async def test_preflight_baseline_excludes_old_code_and_accepts_new_message(
    httpx_mock,
):
    provider = _provider()
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={"access_token": "access-token"},
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
    _add_mailbox_snapshot(httpx_mock, inbox=[old], junk=[])
    _add_mailbox_snapshot(httpx_mock, inbox=[new, old], junk=[])

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
    spoof = {
        "id": "spoof",
        "subject": "OpenAI verification code 123456",
        "bodyPreview": "123456",
        "body": {"content": "123456"},
        "from": {
            "emailAddress": {"address": "noreply@openai.com.attacker.test"}
        },
    }
    _add_mailbox_snapshot(httpx_mock, inbox=[], junk=[])
    _add_mailbox_snapshot(httpx_mock, inbox=[spoof], junk=[])
    await provider.preflight()
    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_verification_code(timeout=0)

    assert error.value.code is EmailErrorCode.NO_CODE
    assert not any(
        "/v1.0/me/messages/" in str(request.url)
        for request in httpx_mock.get_requests()
    )


async def test_graph_body_hydration_is_bounded_per_lookup(httpx_mock):
    provider = _provider()
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={"access_token": "access-token"},
    )
    messages = [
        {
            "id": f"openai-{index}",
            "receivedDateTime": "2026-07-13T10:00:00Z",
            "subject": "OpenAI account notification",
            "bodyPreview": "No verification code in this preview",
            "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
        }
        for index in range(_MAX_HYDRATED_MESSAGES_PER_LOOKUP + 2)
    ]
    _add_mailbox_snapshot(httpx_mock, inbox=[], junk=[])
    _add_mailbox_snapshot(httpx_mock, inbox=messages, junk=[])
    for _ in range(_MAX_HYDRATED_MESSAGES_PER_LOOKUP):
        httpx_mock.add_response(
            method="GET",
            url=re.compile(
                r"https://graph\.microsoft\.com/v1\.0/me/messages/.*"
            ),
            json={"body": {"content": "Account notice without a code"}},
        )

    await provider.preflight()
    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_verification_code(timeout=0)

    assert error.value.code is EmailErrorCode.NO_CODE
    detail_requests = [
        request
        for request in httpx_mock.get_requests()
        if "/v1.0/me/messages/" in str(request.url)
    ]
    assert len(detail_requests) == _MAX_HYDRATED_MESSAGES_PER_LOOKUP


async def test_preflight_and_fetch_find_only_new_junk_message(httpx_mock):
    provider = _provider()
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={"access_token": "access-token"},
    )
    old_junk = {
        "id": "old-junk",
        "subject": "Your ChatGPT verification code",
        "body": {"content": "Old code 101010"},
        "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
    }
    new_junk = {
        "id": "new-junk",
        "subject": "Your ChatGPT verification code",
        "body": {"content": "New code 202020"},
        "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
    }
    _add_mailbox_snapshot(httpx_mock, inbox=[], junk=[old_junk])
    _add_mailbox_snapshot(httpx_mock, inbox=[], junk=[new_junk, old_junk])

    await provider.preflight()
    code = await provider.fetch_verification_code(timeout=0)

    assert code == "202020"
    assert "old-junk" in provider._baseline_ids
    assert "new-junk" in provider._baseline_ids


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


async def test_fresh_graph_fetch_uses_received_timestamp_without_preflight(
    httpx_mock,
):
    provider = _provider()
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={"access_token": "access-token"},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/inbox/messages.*"
        ),
        json={
            "value": [{
                "id": "fresh-message",
                "receivedDateTime": "2026-07-13T10:00:00Z",
                "subject": "Your ChatGPT verification code",
                "bodyPreview": "Code 654321",
                "body": {"content": "Use 654321 to continue"},
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
            }]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/junkemail/messages.*"
        ),
        json={"value": []},
    )

    result = await provider.fetch_fresh_verification_code(
        not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
        timeout=0,
    )

    assert result.code == "654321"
    assert result.received_at == datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    assert len(result.fingerprint) == 64
    assert "654321" not in result.fingerprint


async def test_fresh_graph_fetch_rejects_unproven_timestamp(httpx_mock):
    provider = _provider()
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={"access_token": "access-token"},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/inbox/messages.*"
        ),
        json={
            "value": [{
                "id": "no-timestamp",
                "subject": "Your ChatGPT verification code 654321",
                "body": {"content": "654321"},
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
            }]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/junkemail/messages.*"
        ),
        json={"value": []},
    )

    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_fresh_verification_code(
            not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
            timeout=0,
        )
    assert error.value.code is EmailErrorCode.NO_CODE


async def test_fresh_graph_fetch_finds_code_only_in_junk(httpx_mock):
    provider = _provider()
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={"access_token": "access-token"},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/inbox/messages.*"
        ),
        json={"value": []},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"https://graph\.microsoft\.com/v1\.0/me/mailFolders/junkemail/messages.*"
        ),
        json={
            "value": [{
                "id": "junk-only",
                "receivedDateTime": "2026-07-13T10:00:00Z",
                "subject": "Your ChatGPT verification code",
                "bodyPreview": "Code 919293",
                "body": {"content": "Use 919293 to continue"},
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
            }]
        },
    )

    result = await provider.fetch_fresh_verification_code(
        not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
        timeout=0,
    )

    assert result.code == "919293"
    requested_urls = [str(request.url) for request in httpx_mock.get_requests()]
    assert any("mailFolders/inbox/messages" in url for url in requested_urls)
    assert any("mailFolders/junkemail/messages" in url for url in requested_urls)


async def test_graph_token_list_and_body_share_the_pinned_proxy(
    monkeypatch,
    httpx_mock,
):
    proxy = BrowserProxy(
        route_id=7,
        proxy_type="http",
        host="127.0.0.1",
        port=3128,
        username="proxy-user",
        password="proxy-password",
        config_revision=4,
    )
    seen_proxies = []
    real_client_factory = graph_module.microsoft_graph_http_client

    def recording_client_factory(*, browser_proxy=None, timeout=15.0):
        seen_proxies.append(browser_proxy)
        return real_client_factory(browser_proxy=browser_proxy, timeout=timeout)

    monkeypatch.setattr(
        graph_module,
        "microsoft_graph_http_client",
        recording_client_factory,
    )
    provider = _provider(browser_proxy=proxy)
    httpx_mock.add_response(
        method="POST",
        url=f"{MICROSOFT_AUTHORITY}/token",
        json={"access_token": "access-token"},
    )
    _add_mailbox_snapshot(httpx_mock, inbox=[], junk=[])
    new_message = {
        "id": "new-message",
        "receivedDateTime": "2026-07-16T10:00:00Z",
        "subject": "Your ChatGPT verification code",
        "bodyPreview": "Open this message for the code",
        "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
    }
    _add_mailbox_snapshot(httpx_mock, inbox=[new_message], junk=[])
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"https://graph\.microsoft\.com/v1\.0/me/messages/new-message.*"
        ),
        json={"body": {"content": "Your code is 424242"}},
    )

    await provider.preflight()
    code = await provider.fetch_verification_code(timeout=0)

    assert code == "424242"
    assert seen_proxies == [proxy, proxy, proxy, proxy]


async def test_graph_proxy_transport_failure_is_typed_and_secret_free(monkeypatch):
    proxy = BrowserProxy(
        route_id=7,
        proxy_type="http",
        host="127.0.0.1",
        port=3128,
        username="proxy-user",
        password="do-not-leak-this-password",
        config_revision=4,
    )

    class BrokenClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise graph_module.httpx.ConnectError(
                "do-not-leak-this-password"
            )

    monkeypatch.setattr(
        graph_module,
        "microsoft_graph_http_client",
        lambda **_kwargs: BrokenClient(),
    )
    provider = _provider(browser_proxy=proxy)

    with pytest.raises(ProxyUnavailableError) as error:
        await provider.preflight()

    assert "do-not-leak-this-password" not in str(error.value)
