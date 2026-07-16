from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.email.outlook_web_provider import (
    OutlookWebProvider,
    _FindItemMetadataCollector,
    _MAX_FIND_ITEM_RESPONSE_BYTES,
    _MessageSnapshot,
    _looks_like_openai_code_candidate,
    _looks_like_openai_code_message,
    _message_fingerprint,
    _is_trusted_microsoft_login_url,
    is_outlook_address,
)
from app.integrations.email.provider import EmailErrorCode, EmailProviderError


def _find_item_payload(*items):
    return {
        "Body": {
            "ResponseMessages": {
                "Items": [
                    {
                        "Items": list(items),
                    }
                ]
            }
        }
    }


def _find_item(
    *,
    item_id="item-1",
    conversation_id="conversation-1",
    received_at="2026-07-13T10:00:01Z",
    sender="noreply@openai.com",
    subject="Your authentication code",
):
    return {
        "ItemId": {"Id": item_id, "ChangeKey": "discarded-change-key"},
        "ConversationId": {"Id": conversation_id},
        "DateTimeReceived": received_at,
        "From": {
            "Mailbox": {
                "Name": "OpenAI",
                "EmailAddress": sender,
            }
        },
        "Subject": subject,
        "Preview": "discarded preview 123456",
        "Body": {"BodyType": "HTML", "Value": "discarded body 123456"},
        "Attachments": [{"Name": "discarded.txt", "Content": "discarded"}],
    }


def _find_item_response(
    payload,
    *,
    url="https://outlook.live.com/owa/service.svc?action=FindItem",
    method="POST",
    resource_type="xhr",
    status=200,
    content_type="application/json; charset=utf-8",
    content_length=True,
):
    body = json.dumps(payload).encode()
    headers = {"content-type": content_type}
    if content_length:
        headers["content-length"] = str(len(body))
    response = MagicMock()
    response.url = url
    response.status = status
    response.headers = headers
    response.request = SimpleNamespace(
        url=url,
        method=method,
        resource_type=resource_type,
    )
    response.body = AsyncMock(return_value=body)
    return response


def _observe_opened_response(collector, response):
    collector.observe_request(response.request)
    collector.observe(response)


async def _collector_with(*items, **collector_kwargs):
    collector = _FindItemMetadataCollector(**collector_kwargs)
    response = _find_item_response(_find_item_payload(*items))
    collector.observe(response)
    await collector.drain()
    return collector, response


async def _attach_opened_response(
    provider,
    page,
    locator,
    *,
    code="654321",
    item_id="opened-item-1",
    conversation_id="conversation-1",
    received_at="2026-07-13T10:00:01Z",
    sender="noreply@openai.com",
    subject="Your authentication code",
):
    collector = _FindItemMetadataCollector()
    provider._find_item_collectors[id(page)] = collector
    item = _find_item(
        item_id=item_id,
        conversation_id=conversation_id,
        received_at=received_at,
        sender=sender,
        subject=subject,
    )
    item["Body"] = {
        "BodyType": "HTML",
        "Value": f"Your authentication code is {code}",
    }
    response = _find_item_response(
        _find_item_payload(item),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )

    async def click(**_kwargs):
        _observe_opened_response(collector, response)

    locator.click = AsyncMock(side_effect=click)
    return collector


async def _snapshot_enriched_from_api(provider, item):
    collector, _response = await _collector_with(item)
    page = MagicMock()
    page.evaluate = AsyncMock(
        return_value=[
            {
                "selector": "[data-item-id]",
                "dom_index": 0,
                "text": "OpenAI Your authentication code",
                "item_id": item["ItemId"]["Id"],
                "conversation_id": item["ConversationId"]["Id"],
                "element_id": "row",
                "datetime": "",
            }
        ]
    )
    root_locator = MagicMock()
    root_locator.nth.return_value = MagicMock()
    page.locator.return_value = root_locator
    provider._find_item_collectors[id(page)] = collector
    try:
        snapshots = await provider._visible_openai_messages(page)
    finally:
        provider._find_item_collectors.pop(id(page), None)
        await collector.close()
    assert len(snapshots) == 1
    return snapshots[0]


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
    scan = AsyncMock(return_value=[old])
    monkeypatch.setattr(provider, "_scan_all_folders", scan)

    await provider.preflight()

    assert provider._baseline_keys == {"old-key"}
    assert provider._scan_all_folders.await_count == 2
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
    provider._baseline_at = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    provider._storage_state = {"cookies": [{"name": "session"}]}

    page = MagicMock()
    context = MagicMock()
    old = _MessageSnapshot("old-key", "OpenAI old code", MagicMock())
    new = _MessageSnapshot(
        "new-key",
        "OpenAI new code",
        MagicMock(),
        received_at=datetime(2026, 7, 13, 10, 0, 1, tzinfo=timezone.utc),
    )

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
    read_code.assert_awaited_once_with(
        page,
        new,
        not_before=datetime(2026, 7, 13, 10, tzinfo=timezone.utc),
    )
    assert provider._storage_state is None


async def test_fetch_rejects_old_message_omitted_from_dynamic_tab_baseline(
    monkeypatch,
):
    provider = OutlookWebProvider(
        "user@hotmail.com",
        "password",
        poll_interval_s=0,
    )
    provider._preflight_complete = True
    provider._baseline_at = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    provider._storage_state = {"cookies": [{"name": "session"}]}
    stale = _MessageSnapshot(
        "missed-old-key",
        "OpenAI old code",
        MagicMock(),
        received_at=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
    )
    page = MagicMock()
    context = MagicMock()

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    read_code = AsyncMock(return_value="111111")
    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider,
        "_scan_all_folders",
        AsyncMock(return_value=[stale]),
    )
    monkeypatch.setattr(provider, "_read_code", read_code)

    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_verification_code(timeout=0)

    assert error.value.code is EmailErrorCode.NO_CODE
    read_code.assert_not_awaited()
    assert "missed-old-key" in provider._baseline_keys


async def test_fetch_rejects_old_undated_row_omitted_from_both_baseline_scans(
    monkeypatch,
):
    provider = OutlookWebProvider(
        "user@hotmail.com",
        "password",
        poll_interval_s=0,
    )
    provider._preflight_complete = True
    provider._baseline_at = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    provider._storage_state = {"cookies": [{"name": "session"}]}
    locator = MagicMock()
    locator.click = AsyncMock()
    omitted_old = _MessageSnapshot(
        "omitted-old-undated-key",
        "OpenAI old authentication code 111111",
        locator,
    )
    page = MagicMock()
    context = MagicMock()

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider,
        "_scan_all_folders",
        AsyncMock(return_value=[omitted_old]),
    )
    monkeypatch.setattr(
        provider,
        "_restore_snapshot",
        AsyncMock(return_value=omitted_old),
    )
    monkeypatch.setattr(
        provider,
        "_has_trusted_openai_sender_header",
        AsyncMock(return_value=True),
    )
    opened_date = AsyncMock(
        return_value=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(provider, "_opened_message_received_at", opened_date)

    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_verification_code(timeout=0)

    assert error.value.code is EmailErrorCode.NO_CODE
    opened_date.assert_not_awaited()
    page.locator.assert_not_called()


async def test_fetch_rejects_dom_only_fresh_opened_header_date(monkeypatch):
    provider = OutlookWebProvider(
        "user@hotmail.com",
        "password",
        poll_interval_s=0,
    )
    provider._preflight_complete = True
    provider._baseline_at = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    provider._storage_state = {"cookies": [{"name": "session"}]}
    locator = MagicMock()
    locator.click = AsyncMock()
    fresh = _MessageSnapshot(
        "fresh-undated-key",
        "OpenAI new authentication code",
        locator,
    )
    page = MagicMock()
    context = MagicMock()
    body = MagicMock()
    body.count = AsyncMock(return_value=1)
    body.all_inner_texts = AsyncMock(
        return_value=["Your authentication code is 654321"]
    )
    page.locator.return_value = body

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider,
        "_scan_all_folders",
        AsyncMock(return_value=[fresh]),
    )
    monkeypatch.setattr(
        provider,
        "_restore_snapshot",
        AsyncMock(return_value=fresh),
    )
    monkeypatch.setattr(
        provider,
        "_has_trusted_openai_sender_header",
        AsyncMock(return_value=True),
    )
    opened_date = AsyncMock(
        return_value=datetime(2026, 7, 13, 10, 0, 1, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(provider, "_opened_message_received_at", opened_date)
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.asyncio.sleep",
        AsyncMock(),
    )

    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_verification_code(timeout=0)

    assert error.value.code is EmailErrorCode.NO_CODE
    opened_date.assert_not_awaited()
    page.locator.assert_not_called()


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
    async def read_code(*_args, **_kwargs):
        provider._last_read_evidence = (received_at, "f" * 64)
        return "654321"

    monkeypatch.setattr(provider, "_read_code", AsyncMock(side_effect=read_code))

    result = await provider.fetch_fresh_verification_code(
        not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
        timeout=0,
    )

    assert result.code == "654321"
    assert result.received_at == received_at
    assert result.fingerprint == "f" * 64


async def test_fresh_fetch_rejects_code_without_exact_opened_evidence(monkeypatch):
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
    read_code.assert_awaited_once_with(
        page,
        unproven,
        not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
    )


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

        async def get_attribute(attribute):
            if attribute == "aria-selected" and current_folder["name"] == folder:
                return "true"
            return "false"

        async def click(**_kwargs):
            assert folder is not None
            current_folder["name"] = folder
            opened_folders.append(folder)

        locator.get_attribute = AsyncMock(side_effect=get_attribute)
        locator.click = AsyncMock(side_effect=click)
        locator.press = AsyncMock(side_effect=lambda *_args, **_kwargs: click())
        return locator

    async def visible_openai_messages(_page, **_view):
        folder = current_folder["name"]
        scanned_folders.append(folder)
        return [junk_message] if folder == "junk" else []

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    page.get_by_role.side_effect = get_by_role
    async def read_junk_code(*_args, **_kwargs):
        provider._last_read_evidence = (received_at, "j" * 64)
        return "515253"

    read_code = AsyncMock(side_effect=read_junk_code)
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
    assert opened_folders == ["junk", "inbox"]
    assert scanned_folders == ["inbox", "junk"]
    assert current_folder["name"] == "inbox"
    read_code.assert_awaited_once_with(
        page,
        junk_message,
        not_before=datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc),
    )


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
        received_at=datetime(2026, 7, 13, 10, 0, 1, tzinfo=timezone.utc),
        folder="junk",
        api_metadata_trusted=True,
        item_id="opened-item-1",
        conversation_id="conversation-1",
        api_sender_address="noreply@openai.com",
    )
    collector = await _attach_opened_response(
        provider,
        page,
        live_locator,
        sender="noreply@openai.com",
    )
    monkeypatch.setattr(provider, "_restore_snapshot", AsyncMock(return_value=current))
    try:
        assert await provider._read_code(page, stored) == "654321"
        stale_locator.click.assert_not_awaited()
        live_locator.click.assert_awaited_once_with(timeout=10_000)
        page.locator.assert_not_called()
    finally:
        provider._find_item_collectors.pop(id(page), None)
        await collector.close()


async def test_read_code_accepts_live_outlook_format_after_exact_api_verification(
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
        received_at=datetime(2026, 7, 13, 10, 0, 1, tzinfo=timezone.utc),
        api_metadata_trusted=True,
        item_id="opened-item-1",
        conversation_id="conversation-1",
        api_sender_address="noreply@tm.openai.com",
    )
    collector = await _attach_opened_response(
        provider,
        page,
        locator,
        sender="noreply@tm.openai.com",
    )
    monkeypatch.setattr(provider, "_restore_snapshot", AsyncMock(return_value=snapshot))
    try:
        assert await provider._read_code(page, snapshot) == "654321"
        locator.click.assert_awaited_once_with(timeout=10_000)
        page.locator.assert_not_called()
    finally:
        provider._find_item_collectors.pop(id(page), None)
        await collector.close()


async def test_read_code_uses_clicked_api_item_not_stale_dom_body(monkeypatch):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = MagicMock()
    locator = MagicMock()
    snapshot = _MessageSnapshot(
        "current-message",
        "OpenAI\nYour authentication code",
        locator,
        received_at=datetime(2026, 7, 13, 10, 0, 1, tzinfo=timezone.utc),
        api_metadata_trusted=True,
        item_id="opened-item-1",
        conversation_id="conversation-1",
        api_sender_address="noreply@openai.com",
    )
    stale_dom_body = MagicMock()
    stale_dom_body.all_inner_texts = AsyncMock(
        return_value=["Your old authentication code is 111111"]
    )
    page.locator.return_value = stale_dom_body
    collector = await _attach_opened_response(
        provider,
        page,
        locator,
        code="654321",
    )
    monkeypatch.setattr(
        provider,
        "_restore_snapshot",
        AsyncMock(return_value=snapshot),
    )
    try:
        assert await provider._read_code(page, snapshot) == "654321"
        page.locator.assert_not_called()
        stale_dom_body.all_inner_texts.assert_not_awaited()
    finally:
        provider._find_item_collectors.pop(id(page), None)
        await collector.close()


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
    locator.click.assert_not_awaited()
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
    locator.click.assert_not_awaited()
    page.locator.assert_not_called()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            ["2026-07-13T10:00:01Z", "2026-07-13T12:00:01+02:00"],
            datetime(2026, 7, 13, 10, 0, 1, tzinfo=timezone.utc),
        ),
        (["2026-07-13T10:00:01"], None),
        (["not-a-date"], None),
        (
            ["2026-07-13T10:00:01Z", "2026-07-13T10:00:02Z"],
            None,
        ),
    ],
)
async def test_opened_message_date_requires_one_timezone_aware_value(
    monkeypatch,
    values,
    expected,
):
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=values)
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.asyncio.sleep",
        AsyncMock(),
    )

    assert await OutlookWebProvider._opened_message_received_at(page) == expected


async def test_opened_message_date_excludes_body_and_requires_active_surface(
    monkeypatch,
):
    page = MagicMock()
    scripts: list[str] = []

    async def evaluate(script):
        scripts.append(script)
        return []

    page.evaluate = evaluate
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.asyncio.sleep",
        AsyncMock(),
    )

    assert await OutlookWebProvider._opened_message_received_at(page) is None
    assert scripts
    assert "readSurfaces.length !== 1" in scripts[0]
    assert "body.contains(element)" in scripts[0]
    assert 'root.querySelectorAll("time[datetime]")' in scripts[0]
    assert "readSurfaces[0] || document" not in scripts[0]


async def test_find_item_metadata_enriches_undated_dom_row():
    provider = OutlookWebProvider("user@hotmail.com", "password")

    snapshot = await _snapshot_enriched_from_api(provider, _find_item())

    assert snapshot.received_at == datetime(
        2026, 7, 13, 10, 0, 1, tzinfo=timezone.utc
    )
    assert snapshot.api_metadata_trusted
    assert not snapshot.api_metadata_rejected
    assert snapshot.fingerprint is not None


async def test_find_item_stale_omitted_undated_row_is_rejected(monkeypatch):
    provider = OutlookWebProvider(
        "user@hotmail.com", "password", poll_interval_s=0
    )
    provider._preflight_complete = True
    provider._baseline_at = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    provider._storage_state = {"cookies": [{"name": "session"}]}
    stale = await _snapshot_enriched_from_api(
        provider,
        _find_item(received_at="2026-07-13T09:59:59Z"),
    )
    page = MagicMock()
    context = MagicMock()

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    read_code = AsyncMock(return_value="111111")
    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider, "_scan_all_folders", AsyncMock(return_value=[stale])
    )
    monkeypatch.setattr(provider, "_read_code", read_code)

    with pytest.raises(EmailProviderError) as error:
        await provider.fetch_verification_code(timeout=0)

    assert error.value.code is EmailErrorCode.NO_CODE
    read_code.assert_not_awaited()


async def test_find_item_fresh_omitted_undated_row_is_accepted(monkeypatch):
    provider = OutlookWebProvider(
        "user@hotmail.com", "password", poll_interval_s=0
    )
    provider._preflight_complete = True
    provider._baseline_at = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    provider._storage_state = {"cookies": [{"name": "session"}]}
    fresh = await _snapshot_enriched_from_api(
        provider,
        _find_item(received_at="2026-07-13T10:00:01Z"),
    )
    page = MagicMock()
    context = MagicMock()

    @asynccontextmanager
    async def mailbox_session():
        yield page, context

    read_code = AsyncMock(return_value="654321")
    monkeypatch.setattr(provider, "_mailbox_session", mailbox_session)
    monkeypatch.setattr(
        provider, "_scan_all_folders", AsyncMock(return_value=[fresh])
    )
    monkeypatch.setattr(provider, "_read_code", read_code)

    assert await provider.fetch_verification_code(timeout=1) == "654321"
    read_code.assert_awaited_once_with(
        page,
        fresh,
        not_before=datetime(2026, 7, 13, 10, tzinfo=timezone.utc),
    )


async def test_find_item_spoofed_from_is_rejected():
    provider = OutlookWebProvider("user@hotmail.com", "password")

    snapshot = await _snapshot_enriched_from_api(
        provider,
        _find_item(sender="attacker@example.com"),
    )

    assert snapshot.received_at is None
    assert not snapshot.api_metadata_trusted
    assert snapshot.api_metadata_rejected


async def test_find_item_sender_is_compared_as_complete_structured_address():
    provider = OutlookWebProvider("user@hotmail.com", "password")

    snapshot = await _snapshot_enriched_from_api(
        provider,
        _find_item(sender="attacker@noreply@openai.com"),
    )

    assert not snapshot.api_metadata_trusted
    assert snapshot.api_metadata_rejected


async def test_find_item_display_name_without_address_is_rejected(
    monkeypatch,
):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    item = _find_item()
    item["From"] = {"Mailbox": {"Name": "OpenAI"}}
    snapshot = await _snapshot_enriched_from_api(provider, item)
    snapshot.locator.click = AsyncMock()
    page = MagicMock()

    monkeypatch.setattr(
        provider, "_restore_snapshot", AsyncMock(return_value=snapshot)
    )
    header_sender = AsyncMock(return_value=False)
    monkeypatch.setattr(
        provider, "_has_trusted_openai_sender_header", header_sender
    )
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.asyncio.sleep", AsyncMock()
    )

    assert not snapshot.api_metadata_trusted
    assert snapshot.api_metadata_rejected
    assert await provider._read_code(page, snapshot) is None
    header_sender.assert_not_awaited()
    page.locator.assert_not_called()


@pytest.mark.parametrize(
    "received_at",
    ["not-a-date", "2026-07-13T10:00:01", None],
)
async def test_find_item_malformed_or_naive_date_is_rejected(received_at):
    provider = OutlookWebProvider("user@hotmail.com", "password")

    snapshot = await _snapshot_enriched_from_api(
        provider,
        _find_item(received_at=received_at),
    )

    assert snapshot.received_at is None
    assert snapshot.api_metadata_rejected


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "https://evil.example/owa/service.svc?action=FindItem"},
        {"url": "http://outlook.live.com/owa/service.svc?action=FindItem"},
        {"url": "https://outlook.live.com/owa/service.svc?action=GetItem"},
        {"url": "https://outlook.live.com/owa/other?action=FindItem"},
        {"method": "GET"},
        {"resource_type": "document"},
        {"status": 302},
        {"content_type": "text/html"},
    ],
)
async def test_find_item_collector_ignores_unrelated_responses(overrides):
    collector = _FindItemMetadataCollector()
    response = _find_item_response(_find_item_payload(_find_item()), **overrides)

    collector.observe(response)
    await collector.drain()

    assert collector.record_count == 0
    response.body.assert_not_awaited()


async def test_find_item_collector_is_bounded_and_does_not_retain_content():
    items = [
        _find_item(
            item_id=f"item-{index}",
            conversation_id=f"conversation-{index}",
        )
        for index in range(5)
    ]
    collector, _response = await _collector_with(*items, max_records=2)

    assert collector.record_count == 2
    assert "discarded preview" not in repr(collector._records)
    assert "discarded body" not in repr(collector._records)

    response_limit = _FindItemMetadataCollector(max_responses=2)
    responses = [
        _find_item_response(
            _find_item_payload(
                _find_item(
                    item_id=f"bounded-item-{index}",
                    conversation_id=f"bounded-conversation-{index}",
                )
            )
        )
        for index in range(3)
    ]
    for response in responses:
        response_limit.observe(response)
    await response_limit.drain()
    assert response_limit.record_count == 2
    responses[2].body.assert_not_awaited()


async def test_find_item_budget_does_not_block_exact_clicked_message():
    collector = _FindItemMetadataCollector(max_responses=1)
    first_find = _find_item_response(_find_item_payload(_find_item()))
    ignored_find = _find_item_response(
        _find_item_payload(
            _find_item(item_id="ignored", conversation_id="ignored")
        )
    )
    collector.observe(first_find)
    await collector.drain()
    collector.observe(ignored_find)
    assert collector.lookup(
        item_id="item-1", conversation_id="conversation-1"
    ) == ("missing", None)

    checkpoint = await collector.checkpoint_opened_message()
    opened = _find_item_response(
        _find_item_payload(_find_item()),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    _observe_opened_response(collector, opened)
    state, record = await asyncio.wait_for(
        collector.opened_message_after(
            checkpoint,
            conversation_id="conversation-1",
            item_id="item-1",
        ),
        timeout=1,
    )

    assert state == "trusted"
    assert record is not None
    assert record.verification_code == "123456"
    ignored_find.body.assert_not_awaited()
    await collector.close()


async def test_find_item_collector_rejects_oversized_response_before_body():
    collector = _FindItemMetadataCollector()
    response = _find_item_response(_find_item_payload(_find_item()))
    response.headers["content-length"] = str(_MAX_FIND_ITEM_RESPONSE_BYTES + 1)

    collector.observe(response)
    await collector.drain()

    assert collector.record_count == 0
    response.body.assert_not_awaited()


async def test_find_item_collector_rejects_oversized_actual_body(monkeypatch):
    collector = _FindItemMetadataCollector()
    response = _find_item_response(
        _find_item_payload(_find_item()), content_length=False
    )
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider._MAX_FIND_ITEM_RESPONSE_BYTES",
        32,
    )
    response.body = AsyncMock(return_value=b"{" + (b" " * 32))

    collector.observe(response)
    await collector.drain()

    assert collector.record_count == 0
    response.body.assert_awaited_once()


async def test_find_item_lookup_is_case_sensitive_and_conversation_is_unambiguous():
    first = _find_item(
        item_id="CaseSensitiveItem",
        conversation_id="shared-conversation",
        received_at="2026-07-13T10:00:01Z",
    )
    second = _find_item(
        item_id="other-item",
        conversation_id="shared-conversation",
        received_at="2026-07-13T10:00:02Z",
    )
    collector, _response = await _collector_with(first, second)

    assert collector.lookup(
        item_id="CaseSensitiveItem", conversation_id="shared-conversation"
    )[0] == "trusted"
    assert collector.lookup(
        item_id="casesensitiveitem", conversation_id=None
    )[0] == "missing"
    assert collector.lookup(
        item_id="unknown-item", conversation_id="shared-conversation"
    )[0] == "missing"
    assert collector.lookup(
        item_id=None, conversation_id="shared-conversation"
    )[0] == "missing"


async def test_find_item_lookup_rejects_normalised_or_missing_item_identity():
    collector, _response = await _collector_with(_find_item())

    assert collector.lookup(
        item_id=" item-1 ", conversation_id="conversation-1"
    ) == ("rejected", None)

    missing_item = _find_item(item_id=None, conversation_id="only-conversation")
    collector, _response = await _collector_with(missing_item)
    assert collector.lookup(
        item_id=None, conversation_id="only-conversation"
    ) == ("rejected", None)


async def test_find_item_conflicting_item_metadata_is_fail_closed():
    first = _find_item(
        item_id="same-item",
        conversation_id="conversation-1",
        received_at="2026-07-13T10:00:01Z",
    )
    second = _find_item(
        item_id="same-item",
        conversation_id="conversation-1",
        received_at="2026-07-13T10:00:02Z",
    )
    collector, _response = await _collector_with(first, second)

    assert collector.lookup(
        item_id="same-item", conversation_id="conversation-1"
    ) == ("rejected", None)


async def test_find_item_collector_close_purges_session_metadata():
    collector, _response = await _collector_with(_find_item())
    assert collector.record_count == 1
    assert collector.pending_count == 0

    await collector.close()

    assert collector.record_count == 0
    assert collector.pending_count == 0
    assert collector.lookup(item_id="item-1", conversation_id=None) == (
        "missing",
        None,
    )


async def test_opened_message_requires_new_response_after_checkpoint():
    collector = _FindItemMetadataCollector()
    old_response = _find_item_response(
        _find_item_payload(_find_item()),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    collector.observe_request(old_response.request)
    checkpoint = await collector.checkpoint_opened_message()
    collector.observe(old_response)

    state, record = await collector.opened_message_after(
        checkpoint,
        conversation_id="conversation-1",
        timeout_s=0.01,
    )

    assert (state, record) == ("missing", None)
    await collector.close()


async def test_opened_message_waits_past_delayed_preclick_response():
    collector = _FindItemMetadataCollector()
    delayed_preclick_response = _find_item_response(
        _find_item_payload(
            _find_item(
                item_id="preclick-item",
                received_at="2026-07-13T10:00:01Z",
            )
        ),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    collector.observe_request(delayed_preclick_response.request)
    checkpoint = await collector.checkpoint_opened_message()

    clicked_response = _find_item_response(
        _find_item_payload(
            _find_item(
                item_id="clicked-item",
                received_at="2026-07-13T10:00:02Z",
            )
        ),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )

    async def deliver_responses():
        # The response to a request that began before the click must never be
        # mistaken for the response caused by the click itself.
        collector.observe(delayed_preclick_response)
        await asyncio.sleep(0)
        _observe_opened_response(collector, clicked_response)

    delivery = asyncio.create_task(deliver_responses())
    state, record = await asyncio.wait_for(
        collector.opened_message_after(
            checkpoint,
            conversation_id="conversation-1",
            not_before=datetime(2026, 7, 13, 10, tzinfo=timezone.utc),
        ),
        timeout=1,
    )
    await delivery

    assert state == "trusted"
    assert record is not None
    assert record.metadata.item_id == "clicked-item"
    await collector.close()


async def test_opened_message_rejects_wrong_conversation():
    collector = _FindItemMetadataCollector()
    checkpoint = await collector.checkpoint_opened_message()
    response = _find_item_response(
        _find_item_payload(_find_item(conversation_id="wrong-conversation")),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    _observe_opened_response(collector, response)

    state, record = await collector.opened_message_after(
        checkpoint,
        conversation_id="conversation-1",
    )

    assert (state, record) == ("rejected", None)
    await collector.close()


async def test_opened_message_rejects_different_item_in_same_conversation():
    collector = _FindItemMetadataCollector()
    checkpoint = await collector.checkpoint_opened_message()
    response = _find_item_response(
        _find_item_payload(_find_item(item_id="different-item")),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    _observe_opened_response(collector, response)

    state, record = await asyncio.wait_for(
        collector.opened_message_after(
            checkpoint,
            conversation_id="conversation-1",
            item_id="expected-item",
        ),
        timeout=1,
    )

    assert (state, record) == ("rejected", None)
    await collector.close()


async def test_opened_message_rejects_multiple_items_in_clicked_conversation():
    collector = _FindItemMetadataCollector()
    checkpoint = await collector.checkpoint_opened_message()
    response = _find_item_response(
        _find_item_payload(
            _find_item(item_id="item-1"),
            _find_item(item_id="item-2"),
        ),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    _observe_opened_response(collector, response)

    state, record = await collector.opened_message_after(
        checkpoint,
        conversation_id="conversation-1",
    )

    assert (state, record) == ("rejected", None)
    await collector.close()


async def test_opened_message_selects_exact_item_from_conversation_history():
    collector = _FindItemMetadataCollector()
    checkpoint = await collector.checkpoint_opened_message()
    response = _find_item_response(
        _find_item_payload(
            _find_item(
                item_id="old-item",
                received_at="2026-07-13T09:00:01Z",
            ),
            _find_item(item_id="current-item"),
        ),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    _observe_opened_response(collector, response)

    state, record = await asyncio.wait_for(
        collector.opened_message_after(
            checkpoint,
            conversation_id="conversation-1",
            item_id="current-item",
        ),
        timeout=1,
    )

    assert state == "trusted"
    assert record is not None
    assert record.metadata.item_id == "current-item"
    await collector.close()


async def test_opened_message_selects_only_fresh_item_without_dom_identity():
    collector = _FindItemMetadataCollector()
    checkpoint = await collector.checkpoint_opened_message()
    response = _find_item_response(
        _find_item_payload(
            _find_item(
                item_id="old-item",
                received_at="2026-07-13T09:59:59Z",
            ),
            _find_item(
                item_id="fresh-item",
                received_at="2026-07-13T10:00:01Z",
            ),
        ),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    _observe_opened_response(collector, response)

    state, record = await asyncio.wait_for(
        collector.opened_message_after(
            checkpoint,
            conversation_id="conversation-1",
            not_before=datetime(2026, 7, 13, 10, tzinfo=timezone.utc),
        ),
        timeout=1,
    )

    assert state == "trusted"
    assert record is not None
    assert record.metadata.item_id == "fresh-item"
    await collector.close()


async def test_opened_message_rejects_two_fresh_items_without_dom_identity():
    collector = _FindItemMetadataCollector()
    checkpoint = await collector.checkpoint_opened_message()
    response = _find_item_response(
        _find_item_payload(
            _find_item(
                item_id="fresh-item-1",
                received_at="2026-07-13T10:00:01Z",
            ),
            _find_item(
                item_id="fresh-item-2",
                received_at="2026-07-13T10:00:02Z",
            ),
        ),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    _observe_opened_response(collector, response)

    state, record = await asyncio.wait_for(
        collector.opened_message_after(
            checkpoint,
            conversation_id="conversation-1",
            not_before=datetime(2026, 7, 13, 10, tzinfo=timezone.utc),
        ),
        timeout=1,
    )

    assert (state, record) == ("rejected", None)
    await collector.close()


@pytest.mark.parametrize(
    "item",
    [
        _find_item(sender="attacker@example.com"),
        _find_item(received_at=None),
        {**_find_item(), "Body": {"BodyType": "HTML", "Value": "No code"}},
    ],
)
async def test_opened_message_requires_sender_date_and_code(item):
    collector = _FindItemMetadataCollector()
    checkpoint = await collector.checkpoint_opened_message()
    response = _find_item_response(
        _find_item_payload(item),
        url=(
            "https://outlook.live.com/owa/service.svc?"
            "action=GetConversationItems"
        ),
    )
    _observe_opened_response(collector, response)

    state, record = await collector.opened_message_after(
        checkpoint,
        conversation_id="conversation-1",
    )

    assert (state, record) == ("rejected", None)
    await collector.close()


async def test_find_item_collector_overflow_invalidates_all_metadata():
    collector, _response = await _collector_with(
        _find_item(item_id="first", conversation_id="conversation-first"),
        _find_item(item_id="second", conversation_id="conversation-second"),
        max_records=1,
    )

    assert collector.record_count == 1
    assert collector.lookup(
        item_id="first", conversation_id="conversation-first"
    ) == ("rejected", None)


async def test_find_item_collector_body_timeout_is_finite_and_fail_closed(monkeypatch):
    collector = _FindItemMetadataCollector()
    response = _find_item_response(_find_item_payload(_find_item()))
    never = asyncio.Event()

    async def never_returns():
        await never.wait()
        return b""

    response.body = AsyncMock(side_effect=never_returns)
    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider._FIND_ITEM_BODY_TIMEOUT_S",
        0.01,
    )

    collector.observe(response)
    await collector.drain()

    assert collector.pending_count == 0
    assert collector.lookup(
        item_id="item-1", conversation_id="conversation-1"
    ) == ("rejected", None)


async def test_mailbox_listener_is_installed_before_open(monkeypatch):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    events = []
    page = SimpleNamespace()
    page.set_default_timeout = lambda _timeout: None
    page.on = lambda event, _handler: events.append(("on", event))
    page.remove_listener = lambda event, _handler: events.append(("off", event))
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        close=AsyncMock(),
    )
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
        close=AsyncMock(),
    )
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
    )

    @asynccontextmanager
    async def fake_async_playwright():
        yield playwright

    async def open_mailbox(opened_page):
        assert opened_page is page
        assert events == [("on", "request"), ("on", "response")]

    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.async_playwright",
        fake_async_playwright,
    )
    monkeypatch.setattr(provider, "_open_mailbox", open_mailbox)

    async with provider._mailbox_session() as (opened_page, _context):
        assert opened_page is page

    assert events == [
        ("on", "request"),
        ("on", "response"),
        ("off", "response"),
        ("off", "request"),
    ]
    assert provider._find_item_collectors == {}


async def test_mailbox_session_closes_browser_when_new_page_fails(monkeypatch):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    context = SimpleNamespace(
        new_page=AsyncMock(side_effect=RuntimeError("new page failed")),
        close=AsyncMock(),
    )
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
        close=AsyncMock(),
    )
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
    )

    @asynccontextmanager
    async def fake_async_playwright():
        yield playwright

    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.async_playwright",
        fake_async_playwright,
    )

    with pytest.raises(RuntimeError, match="new page failed"):
        async with provider._mailbox_session():
            pass

    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    assert provider._find_item_collectors == {}


async def test_mailbox_session_cleanup_continues_when_listener_removal_fails(
    monkeypatch,
):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = SimpleNamespace()
    page.set_default_timeout = lambda _timeout: None
    page.on = lambda _event, _handler: None

    def fail_remove_listener(_event, _handler):
        raise RuntimeError("remove listener failed")

    page.remove_listener = fail_remove_listener
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        close=AsyncMock(),
    )
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
        close=AsyncMock(),
    )
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
    )

    @asynccontextmanager
    async def fake_async_playwright():
        yield playwright

    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.async_playwright",
        fake_async_playwright,
    )
    monkeypatch.setattr(provider, "_open_mailbox", AsyncMock())

    async with provider._mailbox_session() as (opened_page, _context):
        assert opened_page is page

    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    assert provider._find_item_collectors == {}


async def test_mailbox_session_closes_browser_when_context_close_fails(
    monkeypatch,
):
    provider = OutlookWebProvider("user@hotmail.com", "password")
    page = SimpleNamespace()
    page.set_default_timeout = lambda _timeout: None
    page.on = lambda _event, _handler: None
    page.remove_listener = lambda _event, _handler: None
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        close=AsyncMock(side_effect=RuntimeError("context close failed")),
    )
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
        close=AsyncMock(),
    )
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
    )

    @asynccontextmanager
    async def fake_async_playwright():
        yield playwright

    monkeypatch.setattr(
        "app.integrations.email.outlook_web_provider.async_playwright",
        fake_async_playwright,
    )
    monkeypatch.setattr(provider, "_open_mailbox", AsyncMock())

    async with provider._mailbox_session():
        pass

    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    assert provider._find_item_collectors == {}


async def test_sender_header_requires_exactly_one_active_read_surface():
    page = MagicMock()
    scripts = []

    async def evaluate(script):
        scripts.append(script)
        return []

    page.evaluate = evaluate

    assert not await OutlookWebProvider._has_trusted_openai_sender_header(page)
    assert "readSurfaces.length !== 1" in scripts[0]
    assert "readSurfaces[0] || document" not in scripts[0]


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
