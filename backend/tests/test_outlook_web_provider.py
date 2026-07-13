from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.email.outlook_web_provider import (
    OutlookWebProvider,
    _MessageSnapshot,
    _looks_like_openai_code_message,
    _message_fingerprint,
    is_outlook_address,
)
from app.integrations.email.provider import EmailErrorCode, EmailProviderError


@pytest.mark.parametrize(
    "address",
    [
        "user@outlook.com",
        "user@hotmail.com",
        "user@live.com",
        "user@msn.com",
        "USER@HOTMAIL.COM",
    ],
)
def test_recognises_microsoft_consumer_domains(address):
    assert is_outlook_address(address)


def test_does_not_treat_custom_exchange_domain_as_outlook_web():
    assert not is_outlook_address("user@example.org")


@pytest.mark.parametrize(
    "text",
    [
        "OpenAI <noreply@tm.openai.com> Your temporary ChatGPT code",
        "OpenAI Ваш временный код ChatGPT",
        "noreply@openai.com Verification code",
    ],
)
def test_identifies_openai_login_mail(text):
    assert _looks_like_openai_code_message(text)


@pytest.mark.parametrize(
    "text",
    [
        "Newsletter ChatGPT weekly update",
        "Microsoft account security code 123456",
        "OpenAI product announcement",
    ],
)
def test_rejects_non_code_mail(text):
    assert not _looks_like_openai_code_message(text)


def test_fingerprint_changes_when_grouped_conversation_gets_new_preview():
    old = {
        "conversation_id": "same-conversation",
        "item_id": "",
        "element_id": "row",
        "datetime": "2026-07-13T08:00:00Z",
        "text": "OpenAI Your temporary ChatGPT code 111111",
    }
    new = {
        **old,
        "datetime": "2026-07-13T08:01:00Z",
        "text": "OpenAI Your temporary ChatGPT code 222222",
    }
    assert _message_fingerprint(old) != _message_fingerprint(new)
    assert _message_fingerprint(old) == _message_fingerprint(dict(old))


async def test_preflight_baselines_existing_messages_and_reuses_session(monkeypatch):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = MagicMock()
    context = MagicMock()
    context.storage_state = AsyncMock(return_value={"cookies": [{"name": "session"}]})
    old = _MessageSnapshot("old-key", "OpenAI temporary code", MagicMock())

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider,
        "_scan_all_folders",
        AsyncMock(return_value=[old]),
    )

    await provider.preflight()

    assert provider._baseline_keys == {"old-key"}
    assert provider.baseline_at is not None
    assert provider._storage_state == {"cookies": [{"name": "session"}]}


async def test_fetch_reads_only_message_newer_than_baseline(monkeypatch):
    provider = OutlookWebProvider(
        "user@hotmail.com",
        "password",
        poll_interval_s=0,
    )
    provider._preflight_complete = True
    provider._baseline_keys = {"old-key"}
    provider._storage_state = {"cookies": [{"name": "session"}]}

    page = MagicMock()
    context = MagicMock()
    old = _MessageSnapshot("old-key", "OpenAI old code", MagicMock())
    new = _MessageSnapshot("new-key", "OpenAI new code", MagicMock())

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    read_code = AsyncMock(return_value="654321")
    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider,
        "_scan_all_folders",
        AsyncMock(return_value=[old, new]),
    )
    monkeypatch.setattr(provider, "_read_code", read_code)

    assert await provider.fetch_verification_code(timeout=1) == "654321"
    read_code.assert_awaited_once_with(page, new)
    assert provider._storage_state is None


async def test_fetch_without_preflight_never_returns_stale_code():
    provider = OutlookWebProvider("user@hotmail.com", "password")
    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_verification_code(timeout=0)
    assert error.value.code is EmailErrorCode.CONNECTION_FAILED


async def test_security_challenge_is_typed_and_secret_free():
    provider = OutlookWebProvider("user@hotmail.com", "secret-password")
    page = MagicMock()
    page.locator.side_effect = lambda selector: _locator_for(selector)

    async def body_text(_page):
        return "verify your identity"

    provider._safe_body_text = body_text
    with pytest.raises(EmailProviderError) as error:
        await provider._raise_for_login_failure_or_challenge(page)

    assert error.value.code is EmailErrorCode.SECURITY_CHALLENGE
    assert "secret-password" not in error.value.detail
    assert "user@hotmail.com" not in error.value.detail


async def test_send_code_action_is_treated_as_manual_security_challenge():
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = MagicMock()

    def locator_for(selector):
        locator = _locator_for(selector)
        if "Send code" in selector:
            locator.is_visible = AsyncMock(return_value=True)
        return locator

    page.locator.side_effect = locator_for
    provider._safe_body_text = AsyncMock(return_value="")

    with pytest.raises(EmailProviderError) as error:
        await provider._raise_for_login_failure_or_challenge(page)
    assert error.value.code is EmailErrorCode.SECURITY_CHALLENGE


def _locator_for(_selector):
    locator = MagicMock()
    locator.first = locator
    locator.is_visible = AsyncMock(return_value=False)
    return locator
