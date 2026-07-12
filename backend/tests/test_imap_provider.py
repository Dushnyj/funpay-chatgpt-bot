from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.email.imap_provider import IMAPProvider, detect_imap_provider


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
    mock_client.login = AsyncMock()
    mock_client.select = AsyncMock()
    mock_client.logout = AsyncMock()

    # SEARCH возвращает UID
    mock_search_resp = MagicMock()
    mock_search_resp.result = "OK"
    mock_search_resp.lines = [b"1"]
    mock_client.search = AsyncMock(return_value=mock_search_resp)

    # FETCH возвращает тело письма
    mock_fetch_resp = MagicMock()
    mock_fetch_resp.lines = [b"Your verification code is 654321."]
    mock_client.fetch = AsyncMock(return_value=[mock_fetch_resp])

    with patch("app.integrations.email.imap_provider.aioimaplib.IMAP4", return_value=mock_client):
        code = await provider.fetch_verification_code(timeout=5)

    assert code == "654321"


async def test_fetch_verification_code_no_messages_returns_none():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")

    mock_client = AsyncMock()
    mock_client.wait_hello_from_server = AsyncMock()
    mock_client.login = AsyncMock()
    mock_client.select = AsyncMock()
    mock_client.logout = AsyncMock()

    mock_search_resp = MagicMock()
    mock_search_resp.result = "OK"
    mock_search_resp.lines = []
    mock_client.search = AsyncMock(return_value=mock_search_resp)

    with patch("app.integrations.email.imap_provider.aioimaplib.IMAP4", return_value=mock_client):
        code = await provider.fetch_verification_code(timeout=1)

    assert code is None


async def test_fetch_verification_code_handles_connection_error():
    provider = IMAPProvider("user@gmail.com", "pass", "imap.gmail.com")

    mock_client = AsyncMock()
    mock_client.wait_hello_from_server = AsyncMock(side_effect=ConnectionRefusedError("refused"))

    with patch("app.integrations.email.imap_provider.aioimaplib.IMAP4", return_value=mock_client):
        code = await provider.fetch_verification_code(timeout=1)

    assert code is None
