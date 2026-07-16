import asyncio
from datetime import datetime, timezone
import re
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from python_socks import ProxyError, ProxyTimeoutError, ProxyType

from app.integrations.email.imap_provider import (
    IMAPProvider,
    _SocketIMAP4SSL,
    _proxy_connector,
    detect_imap_provider,
)
from app.integrations.email.outlook_web_provider import OutlookWebProvider
from app.integrations.email.provider import EmailErrorCode, EmailProviderError
from app.integrations.playwright.proxy import BrowserProxy, ProxyUnavailableError


def _response(result="OK", lines=None):
    response = MagicMock()
    response.result = result
    response.lines = [] if lines is None else lines
    return response


def _header_response(
    message_id: str,
    *,
    size: int = 1024,
    received_at: str | None = None,
    sender: str = "OpenAI <noreply@tm.openai.com>",
):
    metadata = f"{message_id} FETCH (RFC822.SIZE {size}"
    if received_at is not None:
        metadata += f' INTERNALDATE "{received_at}"'
    metadata += ")"
    headers = (
        f"From: {sender}\r\n"
        "Subject: OpenAI verification code\r\n"
        f"Message-ID: <message-{message_id}@tm.openai.com>\r\n\r\n"
    ).encode()
    return _response(lines=[metadata.encode(), headers])


def test_detect_gmail():
    provider = detect_imap_provider("user@gmail.com", "pass")
    assert isinstance(provider, IMAPProvider)
    assert provider.imap_host == "imap.gmail.com"


def test_detect_outlook():
    provider = detect_imap_provider("user@outlook.com", "pass")
    assert isinstance(provider, OutlookWebProvider)


def test_detect_hotmail():
    provider = detect_imap_provider("user@hotmail.com", "pass")
    assert isinstance(provider, OutlookWebProvider)


async def test_outlook_scan_reacquires_dynamic_tab_without_losing_baseline():
    provider = OutlookWebProvider("user@hotmail.com", "mail-password")

    other_tab = MagicMock()
    other_tab.is_visible = AsyncMock(return_value=True)
    other_tab.get_attribute = AsyncMock(return_value="false")
    other_tab.click = AsyncMock(
        side_effect=[PlaywrightTimeoutError("re-rendered"), None]
    )
    other_tab.press = AsyncMock(return_value=None)
    selected_other_tab = MagicMock()
    selected_other_tab.is_visible = AsyncMock(return_value=True)
    selected_other_tab.get_attribute = AsyncMock(return_value="true")
    focused_tab = MagicMock()
    focused_tab.is_visible = AsyncMock(return_value=True)
    focused_tab.get_attribute = AsyncMock(return_value="true")

    other_result = MagicMock()
    other_result.first = other_tab
    focused_result = MagicMock()
    focused_result.first = focused_tab
    page = MagicMock()
    other_calls = 0

    def get_by_role(_role, *, name):
        nonlocal other_calls
        if "other" not in name.pattern:
            return focused_result
        other_calls += 1
        result = MagicMock()
        result.first = selected_other_tab if other_calls >= 5 else other_tab
        return result

    page.get_by_role.side_effect = get_by_role

    provider._open_mail_folder = AsyncMock(side_effect=[True, False])
    old_other = MagicMock(key="old-other")
    old_focused = MagicMock(key="old-focused")
    provider._visible_openai_messages = AsyncMock(
        side_effect=[[old_other], [old_focused]]
    )

    snapshots = await provider._scan_all_folders(page)

    assert {snapshot.key for snapshot in snapshots} == {
        "old-other",
        "old-focused",
    }
    assert other_tab.click.await_count == 1
    assert all(
        call.kwargs == {"timeout": 3_000}
        for call in other_tab.click.await_args_list
    )
    other_tab.press.assert_awaited_once_with("Enter", timeout=3_000)
    focused_tab.click.assert_not_called()


async def test_outlook_scan_rejects_incomplete_focused_inbox_baseline():
    provider = OutlookWebProvider("user@hotmail.com", "mail-password")

    other_tab = MagicMock()
    other_tab.is_visible = AsyncMock(return_value=True)
    focused_tab = MagicMock()
    focused_tab.is_visible = AsyncMock(return_value=False)
    other_result = MagicMock()
    other_result.first = other_tab
    focused_result = MagicMock()
    focused_result.first = focused_tab
    page = MagicMock()
    page.get_by_role.side_effect = lambda _role, *, name: (
        other_result if "other" in name.pattern else focused_result
    )

    provider._open_mail_folder = AsyncMock(return_value=True)
    provider._visible_openai_messages = AsyncMock()

    with pytest.raises(EmailProviderError) as error:
        await provider._scan_all_folders(page)

    assert error.value.code == EmailErrorCode.TIMEOUT
    provider._visible_openai_messages.assert_not_awaited()


async def test_outlook_scan_restarts_when_tabs_appear_after_single_list_scan():
    provider = OutlookWebProvider("user@hotmail.com", "mail-password")
    page = MagicMock()
    other = re.compile(r"other|другие", re.IGNORECASE)
    focused = re.compile(r"focused|приоритетные", re.IGNORECASE)
    provider._visible_inbox_tabs = AsyncMock(
        side_effect=[[], [other, focused], [other, focused], [other, focused]]
    )
    old_single = MagicMock(key="old-single")
    old_other = MagicMock(key="old-other")
    old_focused = MagicMock(key="old-focused")
    provider._visible_openai_messages = AsyncMock(
        side_effect=[[old_single], [old_other], [old_focused]]
    )
    provider._activate_inbox_tab = AsyncMock()

    snapshots = await provider._scan_inbox_stably(page)

    assert {snapshot.key for snapshot in snapshots} == {
        "old-other",
        "old-focused",
    }
    assert provider._activate_inbox_tab.await_count == 2


async def test_outlook_tab_click_must_result_in_selected_state():
    provider = OutlookWebProvider("user@hotmail.com", "mail-password")
    tab = MagicMock()
    tab.is_visible = AsyncMock(return_value=True)
    tab.get_attribute = AsyncMock(return_value="false")
    tab.click = AsyncMock(return_value=None)
    tab.press = AsyncMock(return_value=None)
    result = MagicMock()
    result.first = tab
    page = MagicMock()
    page.get_by_role.return_value = result

    with pytest.raises(EmailProviderError) as error:
        await provider._activate_inbox_tab(
            page, re.compile(r"other", re.IGNORECASE)
        )

    assert error.value.code == EmailErrorCode.TIMEOUT
    assert tab.click.await_count == 3
    assert tab.press.await_count == 3


def test_detect_yahoo():
    provider = detect_imap_provider("user@yahoo.com", "pass")
    assert provider.imap_host == "imap.mail.yahoo.com"


def test_detect_custom_domain_uses_fallback():
    provider = detect_imap_provider(
        "user@mydomain.com", "pass", fallback_host="mail.mydomain.com"
    )
    assert provider.imap_host == "mail.mydomain.com"


def _browser_proxy(proxy_type: str = "http") -> BrowserProxy:
    return BrowserProxy(
        route_id=17,
        proxy_type=proxy_type,
        host="proxy.example.test",
        port=3128,
        username="proxy-user",
        password="proxy-password",
    )


@pytest.mark.parametrize(
    ("configured_type", "expected_type"),
    [
        ("http", ProxyType.HTTP),
        ("https", ProxyType.HTTP),
        ("socks5", ProxyType.SOCKS5),
    ],
)
def test_imap_proxy_uses_connect_transport(configured_type, expected_type):
    proxy = _browser_proxy(configured_type)

    with patch(
        "app.integrations.email.imap_provider.AsyncioProxy"
    ) as constructor:
        _proxy_connector(proxy)

    constructor.assert_called_once_with(
        proxy_type=expected_type,
        host=proxy.host,
        port=proxy.port,
        username=None if configured_type == "socks5" else proxy.username,
        password=None if configured_type == "socks5" else proxy.password,
        rdns=True,
    )


def _client() -> AsyncMock:
    client = AsyncMock()
    client.wait_hello_from_server = AsyncMock()
    client.login = AsyncMock(return_value=_response())
    client.select = AsyncMock(return_value=_response())
    client.logout = AsyncMock()
    return client


async def test_selected_proxy_reaches_imap_tunnel_and_tls_client():
    proxy = _browser_proxy()
    provider = detect_imap_provider(
        "user@gmail.com", "mail-password", browser_proxy=proxy
    )
    assert isinstance(provider, IMAPProvider)

    connected_socket = MagicMock()
    connector = MagicMock()
    connector.connect = AsyncMock(return_value=connected_socket)
    client = _client()
    client.search = AsyncMock(return_value=_response(lines=[]))
    client_task = asyncio.get_running_loop().create_future()
    client_task.set_result((MagicMock(), client.protocol))
    client._client_task = client_task

    with (
        patch(
            "app.integrations.email.imap_provider._proxy_connector",
            return_value=connector,
        ) as connector_factory,
        patch(
            "app.integrations.email.imap_provider._SocketIMAP4SSL",
            return_value=client,
        ) as imap_constructor,
    ):
        await provider.preflight()

    connector_factory.assert_called_once_with(proxy)
    connector.connect.assert_awaited_once_with(
        dest_host="imap.gmail.com",
        dest_port=993,
        timeout=10.0,
    )
    assert imap_constructor.call_args.kwargs["sock"] is connected_socket
    assert isinstance(
        imap_constructor.call_args.kwargs["ssl_context"], ssl.SSLContext
    )
    assert imap_constructor.call_args.kwargs["host"] == "imap.gmail.com"


async def test_proxy_connected_socket_gets_tls_sni_without_direct_destination_dial():
    loop = asyncio.get_running_loop()
    connected_socket = MagicMock()
    tls_context = ssl.create_default_context()
    create_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))

    with patch.object(loop, "create_connection", create_connection):
        client = _SocketIMAP4SSL(
            host="imap.gmail.com",
            port=993,
            sock=connected_socket,
            ssl_context=tls_context,
        )
        await client._client_task

    create_connection.assert_awaited_once()
    call = create_connection.await_args
    assert len(call.args) == 1  # protocol factory only; no direct host/port dial
    assert call.kwargs == {
        "sock": connected_socket,
        "ssl": tls_context,
        "server_hostname": "imap.gmail.com",
    }


@pytest.mark.parametrize("operation", ["preflight", "fetch", "fetch_fresh"])
@pytest.mark.parametrize(
    "failure",
    [
        ProxyError("proxy authentication failed"),
        ProxyTimeoutError("proxy connection timed out"),
    ],
)
async def test_imap_proxy_failure_is_not_swallowed(operation, failure):
    proxy = _browser_proxy()
    provider = IMAPProvider(
        "user@gmail.com",
        "mail-password",
        "imap.gmail.com",
        proxy=proxy,
    )
    connector = MagicMock()
    connector.connect = AsyncMock(side_effect=failure)

    with patch(
        "app.integrations.email.imap_provider._proxy_connector",
        return_value=connector,
    ):
        with pytest.raises(ProxyUnavailableError) as error:
            if operation == "preflight":
                await provider.preflight()
            elif operation == "fetch":
                await provider.fetch_verification_code(timeout=0)
            else:
                await provider.fetch_fresh_verification_code(
                    not_before=datetime.now(timezone.utc), timeout=0
                )

    assert error.value.code == "proxy_unavailable"
    assert error.value.__cause__ is failure


async def test_fetch_verification_code_returns_code():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.search = AsyncMock(return_value=_response(lines=[b"1"]))
    client.fetch = AsyncMock(side_effect=[
        _header_response("1"),
        _response(lines=[b"Your verification code is 654321."]),
    ])

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ) as constructor:
        code = await provider.fetch_verification_code(timeout=5)

    assert code == "654321"
    assert constructor.call_args.kwargs["ssl_context"] is not None
    client.search.assert_awaited_with("ALL", "FROM", "openai.com")


async def test_fetch_verification_code_no_messages_is_no_code():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.search = AsyncMock(return_value=_response(lines=[]))

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        with pytest.raises(EmailProviderError) as error:
            await provider.fetch_verification_code(timeout=0)
    assert error.value.code is EmailErrorCode.NO_CODE


async def test_fetch_verification_code_classifies_connection_error():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.wait_hello_from_server = AsyncMock(
        side_effect=ConnectionRefusedError("refused")
    )

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        with pytest.raises(EmailProviderError) as error:
            await provider.fetch_verification_code(timeout=1)
    assert error.value.code is EmailErrorCode.CONNECTION_FAILED


@pytest.mark.parametrize("failed_operation", ["login", "select", "search", "fetch"])
async def test_protocol_errors_are_classified(failed_operation):
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.search = AsyncMock(return_value=_response(lines=[b"1"]))
    client.fetch = AsyncMock(side_effect=[
        _header_response("1"),
        _response(lines=[b"code 123456"]),
    ])
    setattr(client, failed_operation, AsyncMock(return_value=_response("NO", [b"denied"])))

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        with pytest.raises(EmailProviderError) as error:
            await provider.fetch_verification_code(timeout=1)
    expected = (
        EmailErrorCode.AUTH_FAILED
        if failed_operation == "login"
        else EmailErrorCode.CONNECTION_FAILED
    )
    assert error.value.code is expected


async def test_preflight_excludes_old_read_message_and_fetches_new_one():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.search = AsyncMock(
        side_effect=[
            _response(lines=[b"1"]),
            _response(lines=[]),
            _response(lines=[b"1 2"]),
        ]
    )
    client.fetch = AsyncMock(side_effect=[
        _header_response("2"),
        _response(lines=[b"code 112233"]),
    ])

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        await provider.preflight()
        assert await provider.fetch_verification_code(timeout=1) == "112233"

    assert client.fetch.await_args_list[-1].args == ("2", "(BODY.PEEK[])")


async def test_preflight_and_fetch_find_only_new_gmail_spam_message():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.search = AsyncMock(side_effect=[
        _response(lines=[]),       # preflight INBOX
        _response(lines=[b"7"]),  # preflight Spam baseline
        _response(lines=[]),       # fetch INBOX
        _response(lines=[b"7 8"]),
    ])
    client.fetch = AsyncMock(side_effect=[
        _header_response("8"),
        _response(lines=[b"OpenAI verification code 808080"]),
    ])

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        await provider.preflight()
        code = await provider.fetch_verification_code(timeout=0)

    assert code == "808080"
    assert provider._baseline_ids == {("[Gmail]/Spam", "7"), ("[Gmail]/Spam", "8")}
    assert client.fetch.await_args_list[-1].args == ("8", "(BODY.PEEK[])")
    selected_mailboxes = [call.args[0] for call in client.select.await_args_list]
    assert selected_mailboxes == [
        "INBOX",
        "INBOX",
        '"[Gmail]/Spam"',
        "INBOX",
        "INBOX",
        '"[Gmail]/Spam"',
    ]


async def test_fetch_decodes_mime_encoded_body():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.search = AsyncMock(return_value=_response(lines=[b"9"]))
    raw_message = (
        b"Subject: Verification code\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"WW91ciBjb2RlIGlzIDc3ODg5OQ==\r\n"
    )
    client.fetch = AsyncMock(side_effect=[
        _header_response("9"),
        _response(lines=[raw_message]),
    ])

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        assert await provider.fetch_verification_code(timeout=1) == "778899"


async def test_oversized_message_is_rejected_before_body_download():
    provider = IMAPProvider("user@example.com", "pass", "imap.example.com")
    client = _client()
    client.search = AsyncMock(return_value=_response(lines=[b"1"]))
    client.fetch = AsyncMock(
        return_value=_header_response("1", size=512 * 1024 + 1)
    )

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        with pytest.raises(EmailProviderError) as error:
            await provider.fetch_verification_code(timeout=0)

    assert error.value.code is EmailErrorCode.NO_CODE
    assert all(
        call.args[1] != "(BODY.PEEK[])" for call in client.fetch.await_args_list
    )


async def test_non_code_message_is_consumed_and_not_downloaded_again():
    provider = IMAPProvider("user@example.com", "pass", "imap.example.com")
    provider._junk_folder_candidates = lambda: ()
    client = _client()
    client.search = AsyncMock(return_value=_response(lines=[b"1"]))
    client.fetch = AsyncMock(
        side_effect=[
            _header_response("1"),
            _response(lines=[b"OpenAI account notice without a code"]),
        ]
    )

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        for _ in range(2):
            with pytest.raises(EmailProviderError) as error:
                await provider.fetch_verification_code(timeout=0)
            assert error.value.code is EmailErrorCode.NO_CODE

    assert client.fetch.await_count == 2
    assert provider._baseline_ids == {("INBOX", "1")}


async def test_fresh_fetch_requires_and_returns_proven_received_at():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.search = AsyncMock(return_value=_response(lines=[b"9"]))
    raw_message = (
        b"Date: Mon, 13 Jul 2026 10:00:00 +0000\r\n"
        b"From: OpenAI <noreply@tm.openai.com>\r\n"
        b"Message-ID: <fresh-code@tm.openai.com>\r\n"
        b"Subject: OpenAI verification code\r\n\r\n"
        b"Your code is 778899\r\n"
    )
    client.fetch = AsyncMock(side_effect=[
        _header_response(
            "9",
            received_at="13-Jul-2026 10:00:00 +0000",
        ),
        _response(lines=[raw_message]),
    ])

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        result = await provider.fetch_fresh_verification_code(
            not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
            timeout=0,
        )

    assert result.code == "778899"
    assert result.received_at == datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    assert len(result.fingerprint) == 64
    assert "778899" not in result.fingerprint


async def test_fresh_fetch_rejects_stale_or_undated_mail():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.select = AsyncMock(side_effect=[
        _response(),
        _response(),
        _response(result="NO"),
    ])
    client.search = AsyncMock(return_value=_response(lines=[b"8 9"]))
    client.fetch = AsyncMock(side_effect=[
        _header_response("9"),
        _header_response(
            "8",
            received_at="13-Jul-2026 08:00:00 +0000",
        ),
    ])

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        with pytest.raises(EmailProviderError) as error:
            await provider.fetch_fresh_verification_code(
                not_before=datetime(2026, 7, 13, 9, tzinfo=timezone.utc),
                timeout=0,
            )
    assert error.value.code is EmailErrorCode.NO_CODE


async def test_fresh_fetch_finds_code_only_in_gmail_spam():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = _client()
    client.select = AsyncMock(side_effect=[
        _response(),  # _connect INBOX
        _response(),  # fresh scan INBOX
        _response(),  # [Gmail]/Spam
    ])
    client.search = AsyncMock(side_effect=[
        _response(lines=[]),
        _response(lines=[b"42"]),
    ])
    raw_message = (
        b"From: OpenAI <noreply@tm.openai.com>\r\n"
        b"Message-ID: <spam-code@tm.openai.com>\r\n"
        b"Subject: OpenAI verification code\r\n\r\n"
        b"Your code is 424242\r\n"
    )
    client.fetch = AsyncMock(side_effect=[
        _header_response(
            "42",
            received_at="13-Jul-2026 10:00:00 +0000",
        ),
        _response(lines=[raw_message]),
    ])

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        result = await provider.fetch_fresh_verification_code(
            not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
            timeout=0,
        )

    assert result.code == "424242"
    selected_mailboxes = [call.args[0] for call in client.select.await_args_list]
    assert selected_mailboxes == ["INBOX", "INBOX", '"[Gmail]/Spam"']
