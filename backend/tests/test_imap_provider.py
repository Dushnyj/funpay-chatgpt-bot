from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.email.imap_provider import IMAPProvider, detect_imap_provider


def _response(result="OK", lines=None):
    response = MagicMock()
    response.result = result
    response.lines = [] if lines is None else lines
    return response


def test_detect_gmail():
    p = detect_imap_provider("user@gmail.com", "pass")
    assert isinstance(p, IMAPProvider)
    assert p.imap_host == "imap.gmail.com"


def test_detect_outlook():
    p = detect_imap_provider("user@outlook.com", "pass")
    assert p.imap_host == "outlook.office365.com"


def test_detect_hotmail():
    p = detect_imap_provider("user@hotmail.com", "pass")
    assert p.imap_host == "outlook.office365.com"


def test_detect_yahoo():
    p = detect_imap_provider("user@yahoo.com", "pass")
    assert p.imap_host == "imap.mail.yahoo.com"


def test_detect_custom_domain_uses_fallback():
    # Кастомный домен — fallback на gmail host (можно конфигурировать)
    p = detect_imap_provider("user@mydomain.com", "pass", fallback_host="mail.mydomain.com")
    assert p.imap_host == "mail.mydomain.com"


async def test_fetch_verification_code_returns_code():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")

    # Мокаем aioimaplib.IMAP4
    mock_client = AsyncMock()
    mock_client.wait_hello_from_server = AsyncMock()
    mock_client.login = AsyncMock(return_value=_response())
    mock_client.select = AsyncMock(return_value=_response())
    mock_client.logout = AsyncMock()

    # SEARCH возвращает UID
    mock_search_resp = _response(lines=[b"1"])
    mock_client.search = AsyncMock(return_value=mock_search_resp)

    # FETCH возвращает тело письма
    mock_fetch_resp = _response(lines=[b"Your verification code is 654321."])
    mock_client.fetch = AsyncMock(return_value=mock_fetch_resp)

    with patch("app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL", return_value=mock_client) as ctor:
        code = await provider.fetch_verification_code(timeout=5)

    assert code == "654321"
    assert ctor.call_args.kwargs["ssl_context"] is not None


async def test_fetch_verification_code_no_messages_returns_none():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")

    mock_client = AsyncMock()
    mock_client.wait_hello_from_server = AsyncMock()
    mock_client.login = AsyncMock(return_value=_response())
    mock_client.select = AsyncMock(return_value=_response())
    mock_client.logout = AsyncMock()

    mock_search_resp = _response(lines=[])
    mock_client.search = AsyncMock(return_value=mock_search_resp)

    with patch("app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL", return_value=mock_client):
        code = await provider.fetch_verification_code(timeout=1)

    assert code is None


async def test_fetch_verification_code_handles_connection_error():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")

    mock_client = AsyncMock()
    mock_client.wait_hello_from_server = AsyncMock(side_effect=ConnectionRefusedError("refused"))

    with patch("app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL", return_value=mock_client):
        code = await provider.fetch_verification_code(timeout=1)

    assert code is None


@pytest.mark.parametrize("failed_operation", ["login", "select", "search", "fetch"])
async def test_protocol_errors_return_none(failed_operation):
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")
    client = AsyncMock()
    client.wait_hello_from_server = AsyncMock()
    client.logout = AsyncMock()
    client.login = AsyncMock(return_value=_response())
    client.select = AsyncMock(return_value=_response())
    client.search = AsyncMock(return_value=_response(lines=[b"1"]))
    client.fetch = AsyncMock(return_value=_response(lines=[b"code 123456"]))
    setattr(client, failed_operation, AsyncMock(return_value=_response("NO", [b"denied"])))

    with patch("app.integrations.email.imap_provider.aioimaplib.IMAP4_SSL", return_value=client):
        assert await provider.fetch_verification_code(timeout=1) is None
