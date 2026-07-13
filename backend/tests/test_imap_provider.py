from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

import pytest

from app.integrations.email.imap_provider import IMAPProvider, detect_imap_provider
from app.integrations.email.outlook_web_provider import OutlookWebProvider
from app.integrations.email.provider import EmailErrorCode, EmailProviderError


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


def test_detect_yahoo():
    provider = detect_imap_provider("user@yahoo.com", "pass")
    assert provider.imap_host == "imap.mail.yahoo.com"


def test_detect_custom_domain_uses_fallback():
    provider = detect_imap_provider(
        "user@mydomain.com", "pass", fallback_host="mail.mydomain.com"
    )
    assert provider.imap_host == "mail.mydomain.com"


def _client() -> AsyncMock:
    client = AsyncMock()
    client.wait_hello_from_server = AsyncMock()
    client.login = AsyncMock(return_value=_response())
    client.select = AsyncMock(return_value=_response())
    client.logout = AsyncMock()
    return client


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
