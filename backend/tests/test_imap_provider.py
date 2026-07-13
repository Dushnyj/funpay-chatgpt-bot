from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.email.imap_provider import IMAPProvider, detect_imap_provider
from app.integrations.email.outlook_web_provider import OutlookWebProvider
from app.integrations.email.provider import EmailErrorCode, EmailProviderError


def _response(result="OK", lines=None):
    response = MagicMock()
    response.result = result
    response.lines = [] if lines is None else lines
    return response


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
    client.fetch = AsyncMock(
        return_value=_response(lines=[b"Your verification code is 654321."])
    )

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
    client.fetch = AsyncMock(return_value=_response(lines=[b"code 123456"]))
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
            _response(lines=[b"1 2"]),
        ]
    )
    client.fetch = AsyncMock(return_value=_response(lines=[b"code 112233"]))

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        await provider.preflight()
        assert await provider.fetch_verification_code(timeout=1) == "112233"

    client.fetch.assert_awaited_once_with("2", "(BODY.PEEK[])")


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
    client.fetch = AsyncMock(return_value=_response(lines=[raw_message]))

    with patch(
        "app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL",
        return_value=client,
    ):
        assert await provider.fetch_verification_code(timeout=1) == "778899"
