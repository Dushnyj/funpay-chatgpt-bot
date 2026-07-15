from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.email.outlook_web_provider import (
    OutlookWebProvider,
    _MessageSnapshot,
    _looks_like_openai_code_candidate,
    _looks_like_openai_code_message,
    _message_fingerprint,
    _is_trusted_microsoft_login_url,
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
    ("url", "expected"),
    [
        ("https://login.live.com/oauth20_authorize.srf", True),
        ("https://login.microsoftonline.com/common/oauth2/v2.0/authorize", True),
        ("http://login.live.com/", False),
        ("https://login.live.com.attacker.example/", False),
    ],
)
def test_password_entry_host_allowlist(url, expected):
    assert _is_trusted_microsoft_login_url(url) is expected


@pytest.mark.parametrize(
    "text",
    [
        "OpenAI <noreply@tm.openai.com> Your temporary ChatGPT code",
        "noreply@openai.com Verification code",
        "OpenAI <noreply@tm.openai.com> Your authentication code",
    ],
)
def test_identifies_openai_login_mail(text):
    assert _looks_like_openai_code_message(text)


def test_live_outlook_row_is_only_an_openai_code_candidate():
    text = "OpenAI\nYour authentication code\nPlease use this code to continue."

    assert _looks_like_openai_code_candidate(text)
    assert not _looks_like_openai_code_message(text)


def test_spoofed_display_sender_is_only_a_candidate_until_header_verification():
    text = "OpenAI\nYour authentication code\nCode: 123456"

    assert _looks_like_openai_code_candidate(text)
    assert not _looks_like_openai_code_message(text)


def test_rejects_lookalike_openai_sender_domain():
    text = "noreply@openai.com.evil.example Your authentication code"

    assert not _looks_like_openai_code_message(text)


@pytest.mark.parametrize(
    "text",
    [
        "Newsletter ChatGPT weekly update",
        "Microsoft account security code 123456",
        "Microsoft account team Unusual sign-in activity Review recent activity",
        "OpenAI product announcement",
        "OpenAI Ваш временный код ChatGPT",
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


async def test_password_switch_supports_microsoft_role_button_markup():
    provider = OutlookWebProvider("user@hotmail.com", "password")
    clicked: list[str] = []

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector
            self.first = self

        async def is_visible(self) -> bool:
            return self.selector == (
                "[role='button']:has-text('Используйте свой пароль')"
            )

        async def click(self) -> None:
            clicked.append(self.selector)

        async def wait_for(self, **_kwargs) -> None:
            return None

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    await provider._switch_to_password_if_offered(Page())

    assert clicked == [
        "[role='button']:has-text('Используйте свой пароль')"
    ]


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


async def test_fresh_fetch_reads_timestamped_message_without_preflight(monkeypatch):
    provider = OutlookWebProvider(
        "user@hotmail.com",
        "password",
        poll_interval_s=0,
    )
    page = MagicMock()
    context = MagicMock()
    received_at = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    fresh = _MessageSnapshot(
        "fresh-key",
        "OpenAI fresh code",
        MagicMock(),
        received_at=received_at,
        fingerprint="f" * 64,
    )

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider,
        "_scan_all_folders",
        AsyncMock(return_value=[fresh]),
    )
    monkeypatch.setattr(provider, "_read_code", AsyncMock(return_value="654321"))

    result = await provider.fetch_fresh_verification_code(
        not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
        timeout=0,
    )

    assert result.code == "654321"
    assert result.received_at == received_at
    assert result.fingerprint == "f" * 64


async def test_fresh_fetch_skips_snapshot_without_proven_timestamp(monkeypatch):
    provider = OutlookWebProvider(
        "user@hotmail.com",
        "password",
        poll_interval_s=0,
    )
    page = MagicMock()
    context = MagicMock()
    unproven = _MessageSnapshot(
        "unknown-time",
        "OpenAI code 111111",
        MagicMock(),
    )

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    read_code = AsyncMock(return_value="111111")
    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider,
        "_scan_all_folders",
        AsyncMock(return_value=[unproven]),
    )
    monkeypatch.setattr(provider, "_read_code", read_code)

    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_fresh_verification_code(
            not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
            timeout=0,
        )
    assert error.value.code is EmailErrorCode.NO_CODE
    read_code.assert_not_awaited()


async def test_fresh_fetch_finds_code_only_in_junk_email(monkeypatch):
    provider = OutlookWebProvider(
        "user@hotmail.com",
        "password",
        poll_interval_s=0,
    )
    page = MagicMock()
    context = MagicMock()
    current_folder = {"name": "inbox"}
    opened_folders: list[str] = []
    scanned_folders: list[str] = []
    received_at = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    junk_message = _MessageSnapshot(
        "junk-only",
        "OpenAI fresh code",
        MagicMock(),
        received_at=received_at,
        fingerprint="j" * 64,
    )

    def get_by_role(role, *, name):
        locator = MagicMock()
        locator.first = locator
        pattern = name.pattern.lower()
        folder = None
        if role == "treeitem" and "inbox" in pattern:
            folder = "inbox"
        elif role == "treeitem" and "junk" in pattern:
            folder = "junk"
        locator.is_visible = AsyncMock(return_value=folder is not None)

        async def click(**_kwargs):
            assert folder is not None
            current_folder["name"] = folder
            opened_folders.append(folder)

        locator.click = AsyncMock(side_effect=click)
        return locator

    async def visible_openai_messages(_page, **_view):
        folder = current_folder["name"]
        scanned_folders.append(folder)
        return [junk_message] if folder == "junk" else []

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    page.get_by_role.side_effect = get_by_role
    read_code = AsyncMock(return_value="515253")
    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider,
        "_visible_openai_messages",
        visible_openai_messages,
    )
    monkeypatch.setattr(provider, "_read_code", read_code)
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.asyncio.sleep",
        AsyncMock(),
    )

    result = await provider.fetch_fresh_verification_code(
        not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
        timeout=0,
    )

    assert result.code == "515253"
    assert result.received_at == received_at
    assert opened_folders == ["inbox", "junk", "inbox"]
    assert scanned_folders == ["inbox", "junk"]
    assert current_folder["name"] == "inbox"
    read_code.assert_awaited_once_with(page, junk_message)


async def test_read_code_rebinds_locator_to_captured_mailbox_view(monkeypatch):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = MagicMock()
    stale_locator = MagicMock()
    stale_locator.click = AsyncMock()
    live_locator = MagicMock()
    live_locator.click = AsyncMock()
    stored = _MessageSnapshot(
        "same-message",
        "noreply@openai.com Verification code 123456",
        stale_locator,
        folder="junk",
    )
    current = _MessageSnapshot(
        "same-message",
        "noreply@openai.com Verification code 654321",
        live_locator,
        folder="junk",
    )
    empty_body = MagicMock()
    empty_body.count = AsyncMock(return_value=0)
    page.locator.return_value = empty_body
    page.evaluate = AsyncMock(
        return_value=["From: OpenAI <noreply@openai.com>"]
    )
    monkeypatch.setattr(provider, "_restore_snapshot", AsyncMock(return_value=current))
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.asyncio.sleep",
        AsyncMock(),
    )

    assert await provider._read_code(page, stored) == "654321"
    stale_locator.click.assert_not_awaited()
    live_locator.click.assert_awaited_once_with(timeout=10_000)


async def test_read_code_accepts_live_outlook_format_after_header_verification(
    monkeypatch,
):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = MagicMock()
    locator = MagicMock()
    locator.click = AsyncMock()
    snapshot = _MessageSnapshot(
        "live-message",
        "OpenAI\nYour authentication code\nPlease use the code below.",
        locator,
    )
    body = MagicMock()
    body.count = AsyncMock(return_value=1)
    body.all_inner_texts = AsyncMock(
        return_value=["Your authentication code is 654321"]
    )
    page.locator.return_value = body
    page.evaluate = AsyncMock(
        return_value=["From: OpenAI <noreply@tm.openai.com>"]
    )
    monkeypatch.setattr(provider, "_restore_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.asyncio.sleep",
        AsyncMock(),
    )

    assert await provider._read_code(page, snapshot) == "654321"
    locator.click.assert_awaited_once_with(timeout=10_000)


async def test_read_code_rejects_spoofed_openai_display_sender(monkeypatch):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = MagicMock()
    locator = MagicMock()
    locator.click = AsyncMock()
    snapshot = _MessageSnapshot(
        "spoofed-message",
        "OpenAI\nYour authentication code\nCode: 123456",
        locator,
    )
    page.evaluate = AsyncMock(
        return_value=["From: OpenAI <attacker@example.com>"]
    )
    monkeypatch.setattr(provider, "_restore_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.asyncio.sleep",
        AsyncMock(),
    )

    assert await provider._read_code(page, snapshot) is None
    locator.click.assert_awaited_once_with(timeout=10_000)
    page.locator.assert_not_called()


async def test_read_code_rejects_microsoft_security_notification(monkeypatch):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = MagicMock()
    locator = MagicMock()
    locator.click = AsyncMock()
    snapshot = _MessageSnapshot(
        "security-message",
        "Microsoft account team\nUnusual sign-in activity\nReview recent activity",
        locator,
    )
    page.evaluate = AsyncMock(
        return_value=[
            "From: Microsoft account team "
            "<account-security-noreply@accountprotection.microsoft.com>"
        ]
    )
    monkeypatch.setattr(provider, "_restore_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.asyncio.sleep",
        AsyncMock(),
    )

    assert not _looks_like_openai_code_candidate(snapshot.text)
    assert await provider._read_code(page, snapshot) is None
    locator.click.assert_awaited_once_with(timeout=10_000)
    page.locator.assert_not_called()


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


async def test_password_form_is_not_misclassified_by_hidden_send_code_copy():
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = MagicMock()

    def locator_for(selector):
        locator = _locator_for(selector)
        if "input[name='passwd']" in selector:
            locator.is_visible = AsyncMock(return_value=True)
        if "Send code" in selector:
            locator.is_visible = AsyncMock(return_value=True)
        return locator

    page.locator.side_effect = locator_for
    provider._safe_body_text = AsyncMock(
        return_value="verify your identity or send a code"
    )

    await provider._raise_for_login_failure_or_challenge(page)


def _locator_for(_selector):
    locator = MagicMock()
    locator.first = locator
    locator.is_visible = AsyncMock(return_value=False)
    return locator
