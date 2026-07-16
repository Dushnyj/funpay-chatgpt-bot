from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Request,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.integrations.email.provider import (
    EmailErrorCode,
    EmailProviderError,
    FreshVerificationCode,
    parse_verification_code,
)
from app.integrations.playwright.proxy import (
    BrowserProxy,
    ProxyUnavailableError,
    is_proxy_failure,
)

logger = logging.getLogger(__name__)

_OUTLOOK_URL = "https://outlook.live.com/mail/0/"
_OUTLOOK_LOGIN_URL = "https://outlook.live.com/mail/?prompt=select_account"
_OUTLOOK_DOMAINS = frozenset({"outlook.com", "hotmail.com", "live.com", "msn.com"})
_MICROSOFT_LOGIN_HOSTS = frozenset({
    "login.live.com",
    "login.microsoftonline.com",
})
_OPENAI_SENDER_ADDRESSES = frozenset({
    "noreply@tm.openai.com",
    "noreply@openai.com",
})
_OPENAI_SUBJECT_MARKERS = (
    "temporary chatgpt code",
    "temporary code",
    "authentication code",
    "verification code",
    "login code",
    "sign-in code",
    "временный код chatgpt",
    "временный код",
    "код подтверждения",
    "код входа",
)
_EMAIL_ADDRESS_PATTERN = re.compile(
    r"(?<![\w.+-])([a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)(?![\w.-])",
    re.IGNORECASE,
)
_OPENAI_DISPLAY_NAME_PATTERN = re.compile(
    r"(?<![\w@.])openai(?![\w@.])",
    re.IGNORECASE,
)
_ROW_SELECTOR_CANDIDATES = (
    "[data-item-id]",
    "[data-convid]",
    "[data-conversation-id]",
    "[role='option']",
    "[role='row']",
)
_INBOX_FOLDER_PATTERN = re.compile(r"inbox|входящие", re.IGNORECASE)
_JUNK_FOLDER_PATTERN = re.compile(
    r"junk email|junk|spam|нежелательная почта|спам",
    re.IGNORECASE,
)
_FIND_ITEM_PATH = "/owa/service.svc"
_FIND_ITEM_ACTION = "FindItem"
_GET_CONVERSATION_ITEMS_ACTION = "GetConversationItems"
_FIND_ITEM_CONTENT_TYPES = frozenset({"application/json", "text/json"})
_MAX_FIND_ITEM_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_FIND_ITEM_RESPONSES = 32
_MAX_FIND_ITEM_RECORDS = 512
_MAX_FIND_ITEM_PENDING_RESPONSES = 8
_MAX_FIND_ITEM_JSON_NODES = 4096
_FIND_ITEM_BODY_TIMEOUT_S = 2.0
_FIND_ITEM_QUIET_PERIOD_S = 0.05
_MAX_OUTLOOK_ID_LENGTH = 2048
_MAX_OUTLOOK_SUBJECT_LENGTH = 512
_MAX_OUTLOOK_ADDRESS_LENGTH = 320
_MAX_OUTLOOK_DATETIME_LENGTH = 128
_SKIPPED_FIND_ITEM_FIELDS = frozenset({
    "attachments",
    "attachment",
    "internetmessageheaders",
    "mimecontent",
    "preview",
    "uniquebody",
})


@dataclass(frozen=True, slots=True)
class _MessageSnapshot:
    key: str
    text: str
    locator: Locator
    received_at: datetime | None = None
    fingerprint: str | None = None
    folder: str = "inbox"
    tab_pattern: str | None = None
    api_metadata_trusted: bool = False
    api_metadata_rejected: bool = False
    item_id: str | None = None
    conversation_id: str | None = None
    api_sender_address: str | None = None


@dataclass(frozen=True, slots=True)
class _FindItemMetadata:
    """Validated Outlook metadata kept only for the active browser session."""

    item_id: str | None
    conversation_id: str | None
    received_at: datetime | None
    sender_address: str | None
    trusted: bool


@dataclass(frozen=True, slots=True)
class _OpenedItemMetadata:
    metadata: _FindItemMetadata
    verification_code: str | None


@dataclass(frozen=True, slots=True)
class _OpenedMetadataBatch:
    sequence: int
    records: tuple[_OpenedItemMetadata, ...]


def _bounded_string(value: object, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_length:
        return None
    return value


def _bounded_outlook_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    # Outlook identifiers are opaque and case-sensitive. Whitespace is data,
    # not formatting; reject it rather than normalising two different values.
    if not value or value != value.strip() or len(value) > _MAX_OUTLOOK_ID_LENGTH:
        return None
    return value


def _outlook_api_id(value: object) -> str | None:
    direct = _bounded_outlook_id(value)
    if direct is not None:
        return direct
    if not isinstance(value, dict):
        return None
    for key, nested in value.items():
        if str(key).casefold() == "id":
            return _bounded_outlook_id(nested)
    return None


def _outlook_from_address(value: object, *, depth: int = 0) -> str | None:
    if depth > 3 or not isinstance(value, dict):
        return None
    for key, nested in value.items():
        if str(key).casefold() == "emailaddress":
            return _bounded_string(nested, max_length=_MAX_OUTLOOK_ADDRESS_LENGTH)
    for key, nested in value.items():
        if str(key).casefold() in {"from", "mailbox"}:
            address = _outlook_from_address(nested, depth=depth + 1)
            if address is not None:
                return address
    return None


def _find_item_metadata(value: object) -> _FindItemMetadata | None:
    if not isinstance(value, dict):
        return None
    by_name = {str(key).casefold(): nested for key, nested in value.items()}
    item_id = _outlook_api_id(by_name.get("itemid"))
    conversation_id = _outlook_api_id(by_name.get("conversationid"))
    if item_id is None and conversation_id is None:
        return None

    sender = _outlook_from_address(by_name.get("from"))
    subject = _bounded_string(
        by_name.get("subject"), max_length=_MAX_OUTLOOK_SUBJECT_LENGTH
    )
    # This is a structured EmailAddress field, so compare the complete value.
    # Regex extraction would incorrectly trust strings containing a valid
    # address as a substring. Missing addresses are not trusted API evidence.
    sender_is_acceptable = (
        sender is not None and sender.casefold() in _OPENAI_SENDER_ADDRESSES
    )
    metadata_is_trusted = (
        sender_is_acceptable
        and subject is not None
        and _has_openai_code_subject(subject)
    )

    parsed_dates: set[datetime] = set()
    for name in ("datetimereceived", "receiveddatetime"):
        raw = _bounded_string(
            by_name.get(name), max_length=_MAX_OUTLOOK_DATETIME_LENGTH
        )
        if raw is None:
            continue
        received_at = _received_at({"datetime": raw})
        if received_at is not None:
            parsed_dates.add(received_at)
    return _FindItemMetadata(
        item_id=item_id,
        conversation_id=conversation_id,
        received_at=(next(iter(parsed_dates)) if len(parsed_dates) == 1 else None),
        sender_address=sender.casefold() if sender_is_acceptable else None,
        trusted=metadata_is_trusted and len(parsed_dates) == 1,
    )


def _iter_find_item_metadata(
    payload: object,
) -> tuple[list[_FindItemMetadata], bool]:
    """Extract only allow-listed metadata and never traverse message content."""

    records: list[_FindItemMetadata] = []
    stack: list[tuple[object, int]] = [(payload, 0)]
    visited = 0
    incomplete = False
    while stack:
        if visited >= _MAX_FIND_ITEM_JSON_NODES:
            incomplete = True
            break
        value, depth = stack.pop()
        visited += 1
        if depth > 16:
            incomplete = True
            continue
        if isinstance(value, dict):
            metadata = _find_item_metadata(value)
            if metadata is not None:
                records.append(metadata)
                if len(records) >= _MAX_FIND_ITEM_RECORDS:
                    incomplete = True
                    break
            for key, nested in value.items():
                folded = str(key).casefold()
                if folded in _SKIPPED_FIND_ITEM_FIELDS:
                    continue
                # Service responses also use a top-level `Body` envelope. Only
                # skip an actual message-body value, never that API envelope.
                if folded == "body" and (
                    isinstance(nested, str)
                    or (
                        isinstance(nested, dict)
                        and any(
                            str(body_key).casefold() in {"bodytype", "value"}
                            for body_key in nested
                        )
                    )
                ):
                    continue
                stack.append((nested, depth + 1))
        elif isinstance(value, list):
            stack.extend((nested, depth + 1) for nested in value)
    return records, incomplete


def _opened_body_value(value: dict[object, object]) -> str | None:
    by_name = {str(key).casefold(): nested for key, nested in value.items()}
    for field in ("uniquebody", "body"):
        candidate = by_name.get(field)
        if isinstance(candidate, str):
            return candidate if len(candidate) <= _MAX_FIND_ITEM_RESPONSE_BYTES else None
        if not isinstance(candidate, dict):
            continue
        for key, nested in candidate.items():
            if (
                str(key).casefold() == "value"
                and isinstance(nested, str)
                and len(nested) <= _MAX_FIND_ITEM_RESPONSE_BYTES
            ):
                return nested
    return None


def _iter_opened_item_metadata(
    payload: object,
) -> tuple[list[_OpenedItemMetadata], bool]:
    """Extract one-click message evidence without retaining API body text."""

    records: list[_OpenedItemMetadata] = []
    stack: list[tuple[object, int]] = [(payload, 0)]
    visited = 0
    incomplete = False
    while stack:
        if visited >= _MAX_FIND_ITEM_JSON_NODES:
            incomplete = True
            break
        value, depth = stack.pop()
        visited += 1
        if depth > 16:
            incomplete = True
            continue
        if isinstance(value, dict):
            metadata = _find_item_metadata(value)
            if metadata is not None and metadata.item_id is not None:
                body_text = _opened_body_value(value)
                code = (
                    parse_verification_code(body_text)
                    if body_text is not None
                    else None
                )
                body_text = None
                records.append(
                    _OpenedItemMetadata(
                        metadata=metadata,
                        verification_code=code,
                    )
                )
                if len(records) >= _MAX_FIND_ITEM_RECORDS:
                    incomplete = True
                    break
            for key, nested in value.items():
                folded = str(key).casefold()
                if folded in _SKIPPED_FIND_ITEM_FIELDS:
                    continue
                if folded == "body" and (
                    isinstance(nested, str)
                    or (
                        isinstance(nested, dict)
                        and any(
                            str(body_key).casefold() in {"bodytype", "value"}
                            for body_key in nested
                        )
                    )
                ):
                    continue
                stack.append((nested, depth + 1))
        elif isinstance(value, list):
            stack.extend((nested, depth + 1) for nested in value)
    return records, incomplete


class _FindItemMetadataCollector:
    """Bounded, session-local collector for Outlook FindItem responses."""

    def __init__(
        self,
        *,
        max_records: int = _MAX_FIND_ITEM_RECORDS,
        max_pending: int = _MAX_FIND_ITEM_PENDING_RESPONSES,
        max_responses: int = _MAX_FIND_ITEM_RESPONSES,
    ) -> None:
        self._max_records = max(1, min(max_records, _MAX_FIND_ITEM_RECORDS))
        self._max_pending = max(
            1, min(max_pending, _MAX_FIND_ITEM_PENDING_RESPONSES)
        )
        self._max_responses = max(
            1, min(max_responses, _MAX_FIND_ITEM_RESPONSES)
        )
        self._records: list[_FindItemMetadata] = []
        self._opened_batches: list[_OpenedMetadataBatch] = []
        self._pending: set[asyncio.Task[None]] = set()
        self._opened_event = asyncio.Event()
        self._opened_sequence = 0
        self._opened_requests: dict[int, int] = {}
        self._find_response_count = 0
        self._opened_response_count = 0
        self._closed = False
        self._find_exhausted = False
        self._find_invalidated = False
        self._opened_invalidated = False

    @property
    def record_count(self) -> int:
        return len(self._records)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def observe(self, response: Response) -> None:
        request_action = self._request_action(response.request)
        action = self._response_action(response)
        if self._closed:
            return
        if action is None:
            if request_action == _GET_CONVERSATION_ITEMS_ACTION:
                self._opened_requests.pop(id(response.request), None)
                self._invalidate(_GET_CONVERSATION_ITEMS_ACTION)
            return
        is_opened = action == _GET_CONVERSATION_ITEMS_ACTION
        response_count = (
            self._opened_response_count
            if is_opened
            else self._find_response_count
        )
        if response_count >= self._max_responses:
            if is_opened:
                self._invalidate(action)
            else:
                # FindItem is only an optional list-enrichment source. Once
                # its bounded budget is exhausted, exact post-click evidence
                # may still prove a candidate safely.
                self._find_exhausted = True
            return
        if len(self._pending) >= self._max_pending:
            self._invalidate(action)
            return
        if not is_opened and len(self._records) >= self._max_records:
            self._find_invalidated = True
            return
        sequence = 0
        if is_opened:
            sequence = self._opened_requests.pop(id(response.request), 0)
            if sequence <= 0:
                self._invalidate(action)
                return
            self._opened_response_count += 1
        else:
            self._find_response_count += 1
        task = asyncio.create_task(
            self._consume_response(response, action=action, sequence=sequence)
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def observe_request(self, request: Request) -> None:
        if (
            self._closed
            or self._opened_invalidated
            or self._request_action(request) != _GET_CONVERSATION_ITEMS_ACTION
        ):
            return
        request_key = id(request)
        if (
            request_key in self._opened_requests
            or len(self._opened_requests) >= self._max_responses
        ):
            self._invalidate(_GET_CONVERSATION_ITEMS_ACTION)
            return
        self._opened_sequence += 1
        self._opened_requests[request_key] = self._opened_sequence

    async def drain(self) -> None:
        while True:
            while self._pending:
                pending = tuple(self._pending)
                await asyncio.gather(*pending, return_exceptions=True)
                # Do not rely only on done callbacks to mutate the set. A
                # GetConversationItems task can wake ``opened_message_after``
                # immediately before its callback is scheduled; repeatedly
                # gathering an already-finished task would then starve that
                # callback and spin forever.
                self._pending.difference_update(pending)
            # Playwright dispatches response callbacks asynchronously. Require
            # a short quiet barrier so a just-queued relevant response cannot
            # arrive immediately after an apparently empty drain.
            await asyncio.sleep(_FIND_ITEM_QUIET_PERIOD_S)
            if not self._pending:
                return

    async def close(self) -> None:
        self._closed = True
        pending = tuple(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending.clear()
        self._records.clear()
        self._opened_batches.clear()
        self._opened_requests.clear()
        self._find_response_count = 0
        self._opened_response_count = 0
        self._find_exhausted = False
        self._find_invalidated = False
        self._opened_invalidated = False
        self._opened_event.set()

    async def checkpoint_opened_message(self) -> int:
        await self.drain()
        self._opened_event.clear()
        return self._opened_sequence

    async def opened_message_after(
        self,
        checkpoint: int,
        *,
        conversation_id: object,
        item_id: object = None,
        received_at: datetime | None = None,
        not_before: datetime | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[str, _OpenedItemMetadata | None]:
        conversation = _bounded_outlook_id(conversation_id)
        item = _bounded_outlook_id(item_id)
        if conversation is None or self._closed or self._opened_invalidated:
            return "rejected", None
        if item_id is not None and item_id != "" and item is None:
            return "rejected", None
        expected_received_at: datetime | None = None
        if received_at is not None:
            if not isinstance(received_at, datetime) or received_at.tzinfo is None:
                return "rejected", None
            expected_received_at = received_at.astimezone(UTC)
        cutoff: datetime | None = None
        if not_before is not None:
            if not isinstance(not_before, datetime) or not_before.tzinfo is None:
                return "rejected", None
            cutoff = not_before.astimezone(UTC)
        deadline = time.monotonic() + timeout_s
        while not any(
            batch.sequence > checkpoint for batch in self._opened_batches
        ):
            if self._closed or self._opened_invalidated:
                return "rejected", None
            self._opened_event.clear()
            if any(
                batch.sequence > checkpoint for batch in self._opened_batches
            ):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "missing", None
            try:
                await asyncio.wait_for(
                    self._opened_event.wait(), timeout=remaining
                )
            except TimeoutError:
                return "missing", None
            await self.drain()
        if self._closed or self._opened_invalidated:
            return "rejected", None
        batches = [
            batch for batch in self._opened_batches if batch.sequence > checkpoint
        ]
        if len(batches) != 1:
            return ("missing", None) if not batches else ("rejected", None)
        item_records = [
            record
            for record in batches[0].records
            if record.metadata.item_id is not None
        ]
        if not item_records or any(
            record.metadata.conversation_id != conversation
            for record in item_records
        ):
            return "rejected", None
        if item is not None:
            candidates = [
                record
                for record in item_records
                if record.metadata.item_id == item
            ]
        elif expected_received_at is not None:
            candidates = [
                record
                for record in item_records
                if record.metadata.received_at == expected_received_at
            ]
        elif cutoff is not None:
            candidates = [
                record
                for record in item_records
                if record.metadata.trusted
                and record.verification_code is not None
                and record.metadata.received_at is not None
                and record.metadata.received_at >= cutoff
            ]
        else:
            candidates = item_records
        if len(candidates) != 1:
            return "rejected", None
        record = candidates[0]
        if (
            not record.metadata.trusted
            or record.verification_code is None
        ):
            return "rejected", None
        return "trusted", record

    async def _consume_response(
        self,
        response: Response,
        *,
        action: str,
        sequence: int,
    ) -> None:
        if (
            action == _FIND_ITEM_ACTION
            and len(self._records) >= self._max_records
        ):
            self._find_invalidated = True
            return
        try:
            content_length = response.headers.get("content-length")
            if content_length is not None:
                if not content_length.isdecimal():
                    self._invalidate(action)
                    return
                if int(content_length) > _MAX_FIND_ITEM_RESPONSE_BYTES:
                    self._invalidate(action)
                    return
            body = await asyncio.wait_for(
                response.body(), timeout=_FIND_ITEM_BODY_TIMEOUT_S
            )
            if len(body) > _MAX_FIND_ITEM_RESPONSE_BYTES:
                self._invalidate(action)
                return
            payload = json.loads(body)
            body = b""
            if action == _GET_CONVERSATION_ITEMS_ACTION:
                opened_records, incomplete = _iter_opened_item_metadata(payload)
                records: list[_FindItemMetadata] = []
            else:
                records, incomplete = _iter_find_item_metadata(payload)
                opened_records = []
            payload = None
        except asyncio.CancelledError:
            raise
        except Exception:
            self._invalidate(action)
            return

        if incomplete:
            self._invalidate(action)
            return

        if action == _GET_CONVERSATION_ITEMS_ACTION:
            self._opened_batches.append(
                _OpenedMetadataBatch(
                    sequence=sequence,
                    records=tuple(opened_records),
                )
            )
            self._opened_event.set()
            return

        for record in records:
            if len(self._records) >= self._max_records:
                self._find_invalidated = True
                break
            if record not in self._records:
                self._records.append(record)

    def _invalidate(self, action: str) -> None:
        if action == _GET_CONVERSATION_ITEMS_ACTION:
            self._opened_invalidated = True
            self._opened_event.set()
        else:
            self._find_invalidated = True

    @staticmethod
    def _request_action(request: Request) -> str | None:
        try:
            parsed = urlsplit(request.url)
            query = parse_qs(parsed.query, keep_blank_values=False)
            is_supported = (
                request.resource_type in {"xhr", "fetch"}
                and request.method == "POST"
                and parsed.scheme.casefold() == "https"
                and (parsed.hostname or "").casefold() == "outlook.live.com"
                and parsed.username is None
                and parsed.password is None
                and parsed.port in {None, 443}
                and parsed.path.rstrip("/") == _FIND_ITEM_PATH
                and not parsed.fragment
                and query.get("action")
                in ([_FIND_ITEM_ACTION], [_GET_CONVERSATION_ITEMS_ACTION])
            )
            return query["action"][0] if is_supported else None
        except Exception:
            return None

    @classmethod
    def _response_action(cls, response: Response) -> str | None:
        try:
            action = cls._request_action(response.request)
            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().casefold()
            is_supported = (
                action is not None
                and response.url == response.request.url
                and response.status == 200
                and media_type in _FIND_ITEM_CONTENT_TYPES
            )
            return action if is_supported else None
        except Exception:
            return None

    def lookup(
        self,
        *,
        item_id: object,
        conversation_id: object,
    ) -> tuple[str, _FindItemMetadata | None]:
        if self._closed:
            return "missing", None
        if self._find_invalidated:
            return "rejected", None
        if self._find_exhausted:
            return "missing", None
        item = _bounded_outlook_id(item_id)
        conversation = _bounded_outlook_id(conversation_id)
        if item_id is not None and item_id != "" and item is None:
            return "rejected", None
        if conversation_id is not None and conversation_id != "" and conversation is None:
            return "rejected", None
        if item is not None:
            matches = {record for record in self._records if record.item_id == item}
            if len(matches) == 1:
                record = next(iter(matches))
                if (
                    conversation is not None
                    and record.conversation_id is not None
                    and record.conversation_id != conversation
                ):
                    return "rejected", None
                return (
                    ("trusted", record)
                    if record.trusted
                    else ("rejected", None)
                )
            if len(matches) > 1:
                return "rejected", None
            # A DOM-provided ItemId is the authoritative correlation key. Do
            # not weaken that exact match by falling back to ConversationId.
            return "missing", None
        if conversation is not None:
            matches = {
                record
                for record in self._records
                if record.conversation_id == conversation
            }
            if len(matches) == 1:
                record = next(iter(matches))
                if record.item_id is None:
                    return "rejected", None
                return (
                    ("trusted", record)
                    if record.trusted
                    else ("rejected", None)
                )
            if len(matches) > 1:
                # A conversation row can legitimately group several login
                # mails. The exact click-scoped response plus freshness cutoff
                # performs the final unambiguous selection.
                return "missing", None
        return "missing", None


def is_outlook_address(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return domain in _OUTLOOK_DOMAINS


def _is_trusted_microsoft_login_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in (
        _MICROSOFT_LOGIN_HOSTS
    )


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _has_openai_code_subject(text: str) -> bool:
    normalised = _normalise_text(text)
    return any(marker in normalised for marker in _OPENAI_SUBJECT_MARKERS)


def _has_trusted_openai_sender(texts: list[str] | tuple[str, ...]) -> bool:
    for text in texts:
        addresses = _EMAIL_ADDRESS_PATTERN.findall(str(text))
        if any(
            address.casefold() in _OPENAI_SENDER_ADDRESSES
            for address in addresses
        ):
            return True
    return False


def _exact_header_addresses(
    texts: list[str] | tuple[str, ...],
) -> set[str] | None:
    """Extract a complete, non-conflicting set of header email addresses.

    Every ``@`` must belong to a syntactically valid address match. This keeps
    malformed concatenations from smuggling an allow-listed suffix through the
    regex, while still accepting labels such as ``From: Name <a@example.com>``.
    """

    addresses: set[str] = set()
    for raw in texts:
        text = str(raw)
        matches = list(_EMAIL_ADDRESS_PATTERN.finditer(text))
        spans = tuple(match.span(1) for match in matches)
        if any(
            not any(start <= index < end for start, end in spans)
            for index, character in enumerate(text)
            if character == "@"
        ):
            return None
        addresses.update(match.group(1).casefold() for match in matches)
    return addresses


def _looks_like_openai_code_message(text: str) -> bool:
    has_sender = _has_trusted_openai_sender((text,))
    has_code_subject = _has_openai_code_subject(text)
    # A display name alone is attacker-controlled and is not enough to treat a
    # fully opened message as an OpenAI login message.
    return has_sender and has_code_subject


def _looks_like_openai_code_candidate(text: str) -> bool:
    """Return whether an Outlook list row is worth opening.

    Outlook currently renders the sender as the display name ``OpenAI`` and
    the subject as ``Your authentication code`` without exposing the raw
    address in the list.  A display name is attacker-controlled, so this is
    deliberately only a candidate filter.  ``_read_code`` independently
    verifies the opened message's sender metadata before parsing any code.
    """

    if not _has_openai_code_subject(text):
        return False
    if _has_trusted_openai_sender((text,)):
        return True
    return _OPENAI_DISPLAY_NAME_PATTERN.search(_normalise_text(text)) is not None


def _message_fingerprint(payload: dict[str, Any]) -> str:
    """Return a non-reversible key for one visible Outlook list revision.

    Outlook may group multiple login messages into one conversation.  The
    stable DOM id alone is therefore insufficient: timestamp and visible
    preview are included so a newly delivered code in the same conversation
    becomes a new snapshot.  The raw preview (which may contain a code) is
    never logged or persisted.
    """

    stable = "|".join(
        str(payload.get(name) or "")
        for name in ("item_id", "conversation_id", "element_id", "datetime")
    )
    semantic = _normalise_text(str(payload.get("text") or ""))
    return hashlib.sha256(f"{stable}|{semantic}".encode()).hexdigest()


def _received_at(payload: dict[str, Any]) -> datetime | None:
    """Parse only an explicit timezone-aware HTML ``time[datetime]`` value."""
    parsed: set[datetime] = set()
    for candidate in str(payload.get("datetime") or "").split("|"):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            value = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is not None:
            parsed.add(value.astimezone(UTC))
    return next(iter(parsed)) if len(parsed) == 1 else None


def _fresh_message_fingerprint(
    payload: dict[str, Any],
    received_at: datetime,
) -> str | None:
    # DOM element ids may change between mailbox sessions. At least one
    # Outlook item/conversation id is required for durable per-rental dedupe.
    stable_ids = "|".join(
        value or ""
        for value in (
            _bounded_outlook_id(payload.get("item_id")),
            _bounded_outlook_id(payload.get("conversation_id")),
        )
    )
    if not stable_ids.replace("|", ""):
        return None
    material = f"outlook-web|{stable_ids}|{received_at.isoformat()}"
    return hashlib.sha256(material.encode()).hexdigest()


class OutlookWebProvider:
    """Read new OpenAI verification codes through Outlook Web.

    Microsoft no longer accepts an ordinary account password for IMAP Basic
    authentication.  This provider uses the same interactive web login that a
    human uses, keeps its mailbox session only in process memory and compares
    message snapshots captured immediately before OpenAI can send a code.
    """

    def __init__(
        self,
        email: str,
        password: str,
        *,
        headless: bool = True,
        navigation_timeout_ms: int = 60_000,
        poll_interval_s: float = 2.0,
        proxy: BrowserProxy | None = None,
    ) -> None:
        self.email = email
        self._password = password
        self._headless = headless
        self._navigation_timeout_ms = navigation_timeout_ms
        self._poll_interval_s = poll_interval_s
        self._proxy = proxy
        self._baseline_keys: set[str] = set()
        self._baseline_at: datetime | None = None
        self._storage_state: dict[str, Any] | None = None
        self._preflight_complete = False
        self._find_item_collectors: dict[int, _FindItemMetadataCollector] = {}
        self._last_read_evidence: tuple[datetime, str] | None = None

    @property
    def baseline_at(self) -> datetime | None:
        """Timestamp of the last in-memory baseline (never persisted)."""

        return self._baseline_at

    async def preflight(self) -> None:
        """Log in and remember every currently visible OpenAI code message."""

        # A repeated preflight must never retain a previously valid mailbox
        # snapshot if the new scan fails part-way through.
        self._preflight_complete = False
        self._baseline_keys.clear()
        self._baseline_at = None
        self._storage_state = None
        try:
            async with self._mailbox_session() as (page, context):
                snapshots = await self._scan_all_folders(page)
                # Confirm the mailbox once more after the SPA has had another
                # render turn. The union protects a 0-tabs -> Focused/Other
                # transition during initial mailbox hydration.
                await asyncio.sleep(0.35)
                confirmed = await self._scan_all_folders(page)
                snapshots = list(
                    {
                        snapshot.key: snapshot
                        for snapshot in (*snapshots, *confirmed)
                    }.values()
                )
                self._baseline_keys = {snapshot.key for snapshot in snapshots}
                self._baseline_at = datetime.now(UTC)
                self._storage_state = await context.storage_state()
                self._preflight_complete = True
        except EmailProviderError:
            raise
        except ProxyUnavailableError:
            raise
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            raise EmailProviderError(
                EmailErrorCode.TIMEOUT,
                "Outlook Web не ответил за отведённое время.",
            ) from exc
        except Exception as exc:
            if self._proxy is not None and is_proxy_failure(exc):
                raise ProxyUnavailableError(
                    "Маршрут входа в Outlook через прокси недоступен."
                ) from exc
            logger.warning("Outlook Web preflight failed", exc_info=False)
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Не удалось открыть почтовый ящик через Outlook Web.",
            ) from exc

    async def fetch_verification_code(self, timeout: float = 60.0) -> str | None:
        """Wait for a new OpenAI message and return its six-digit code."""

        if not self._preflight_complete:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Перед ожиданием кода не выполнена проверка Outlook Web.",
            )

        deadline = time.monotonic() + timeout
        baseline_at = self._baseline_at
        if baseline_at is None:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Проверка Outlook Web не сохранила время исходного снимка.",
            )
        # Outlook's HTML timestamp can have second precision while Python's
        # baseline includes microseconds. The stable double scan remains the
        # primary guard; this cutoff independently rejects older omitted rows.
        freshness_cutoff = baseline_at.replace(microsecond=0)
        try:
            async with self._mailbox_session() as (page, _context):
                while True:
                    snapshots = await self._scan_all_folders(page)
                    for snapshot in snapshots:
                        if snapshot.key in self._baseline_keys:
                            continue

                        # Mark the list revision before opening it, so a
                        # non-code OpenAI mail cannot be retried forever.
                        self._baseline_keys.add(snapshot.key)
                        if (
                            snapshot.received_at is not None
                            and snapshot.received_at < freshness_cutoff
                        ):
                            continue
                        code = await self._read_code(
                            page,
                            snapshot,
                            not_before=freshness_cutoff,
                        )
                        if code is not None:
                            return code

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise EmailProviderError(
                            EmailErrorCode.NO_CODE,
                            "Новое письмо с кодом OpenAI не пришло за отведённое время.",
                        )
                    await asyncio.sleep(min(self._poll_interval_s, remaining))
        except EmailProviderError:
            raise
        except ProxyUnavailableError:
            raise
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            raise EmailProviderError(
                EmailErrorCode.TIMEOUT,
                "Outlook Web не ответил при ожидании письма.",
            ) from exc
        except Exception as exc:
            if self._proxy is not None and is_proxy_failure(exc):
                raise ProxyUnavailableError(
                    "Маршрут входа в Outlook через прокси недоступен."
                ) from exc
            logger.warning("Outlook Web code lookup failed", exc_info=False)
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Не удалось получить письмо через Outlook Web.",
            ) from exc
        finally:
            # Microsoft cookies are needed only between preflight and fetch.
            self._storage_state = None

    async def fetch_fresh_verification_code(
        self,
        *,
        not_before: datetime,
        timeout: float = 10.0,
    ) -> FreshVerificationCode:
        """Read a provably recent mailbox code without taking a new baseline."""
        cutoff = (
            not_before.replace(tzinfo=UTC)
            if not_before.tzinfo is None
            else not_before.astimezone(UTC)
        )
        deadline = time.monotonic() + timeout
        try:
            async with self._mailbox_session() as (page, _context):
                while True:
                    snapshots = sorted(
                        await self._scan_all_folders(page),
                        key=lambda item: item.received_at or datetime.min.replace(tzinfo=UTC),
                        reverse=True,
                    )
                    for snapshot in snapshots:
                        if (
                            snapshot.received_at is not None
                            and snapshot.received_at < cutoff
                        ):
                            continue
                        code = await self._read_code(
                            page, snapshot, not_before=cutoff
                        )
                        if code is not None:
                            evidence = self._last_read_evidence
                            if evidence is None:
                                continue
                            return FreshVerificationCode(
                                code=code,
                                received_at=evidence[0],
                                fingerprint=evidence[1],
                            )

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise EmailProviderError(
                            EmailErrorCode.NO_CODE,
                            "Свежее письмо с кодом OpenAI не найдено.",
                        )
                    await asyncio.sleep(min(self._poll_interval_s, remaining))
        except EmailProviderError:
            raise
        except ProxyUnavailableError:
            raise
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            raise EmailProviderError(
                EmailErrorCode.TIMEOUT,
                "Outlook Web не ответил при чтении свежего письма.",
            ) from exc
        except Exception as exc:
            if self._proxy is not None and is_proxy_failure(exc):
                raise ProxyUnavailableError(
                    "Маршрут входа в Outlook через прокси недоступен."
                ) from exc
            logger.warning("Outlook Web fresh-code lookup failed", exc_info=False)
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Не удалось прочитать свежее письмо через Outlook Web.",
            ) from exc

    @asynccontextmanager
    async def _mailbox_session(
        self,
    ) -> AsyncIterator[tuple[Page, BrowserContext]]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self._headless,
                args=["--disable-dev-shm-usage"],
                proxy=(
                    self._proxy.as_playwright()
                    if self._proxy is not None
                    else None
                ),
            )
            context: BrowserContext | None = None
            page: Page | None = None
            collector: _FindItemMetadataCollector | None = None
            observe_find_item: Any = None
            observe_get_conversation_request: Any = None
            response_listener_registered = False
            request_listener_registered = False
            try:
                context = await browser.new_context(
                    storage_state=self._storage_state,
                    locale="ru-RU",
                )
                page = await context.new_page()
                page.set_default_timeout(self._navigation_timeout_ms)
                collector = _FindItemMetadataCollector()
                self._find_item_collectors[id(page)] = collector

                def observe_response(response: Response) -> None:
                    collector.observe(response)

                def observe_request(request: Request) -> None:
                    collector.observe_request(request)

                observe_find_item = observe_response
                observe_get_conversation_request = observe_request
                if callable(getattr(page, "on", None)):
                    page.on("request", observe_get_conversation_request)
                    request_listener_registered = True
                    page.on("response", observe_find_item)
                    response_listener_registered = True
                await self._open_mailbox(page)
                yield page, context
            finally:
                if (
                    page is not None
                    and response_listener_registered
                    and callable(getattr(page, "remove_listener", None))
                ):
                    try:
                        page.remove_listener("response", observe_find_item)
                    except Exception:
                        logger.warning(
                            "Outlook Web response-listener cleanup failed",
                            exc_info=False,
                        )
                if (
                    page is not None
                    and request_listener_registered
                    and callable(getattr(page, "remove_listener", None))
                ):
                    try:
                        page.remove_listener(
                            "request", observe_get_conversation_request
                        )
                    except Exception:
                        logger.warning(
                            "Outlook Web request-listener cleanup failed",
                            exc_info=False,
                        )
                if collector is not None:
                    try:
                        await collector.close()
                    except Exception:
                        logger.warning(
                            "Outlook Web metadata cleanup failed",
                            exc_info=False,
                        )
                if page is not None:
                    self._find_item_collectors.pop(id(page), None)
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        logger.warning(
                            "Outlook Web context cleanup failed",
                            exc_info=False,
                        )
                try:
                    await browser.close()
                except Exception:
                    logger.warning(
                        "Outlook Web browser cleanup failed",
                        exc_info=False,
                    )

    async def _open_mailbox(self, page: Page) -> None:
        await page.goto(
            _OUTLOOK_URL,
            wait_until="domcontentloaded",
            timeout=self._navigation_timeout_ms,
        )

        if await self._mailbox_is_ready(page):
            return

        await self._raise_for_login_failure_or_challenge(page)
        await self._start_login_from_landing(page)
        email_input = page.locator("input[name='loginfmt'], input[type='email']").first
        try:
            await email_input.wait_for(timeout=20_000)
        except PlaywrightTimeoutError:
            await self._raise_for_login_failure_or_challenge(page)
            raise
        await email_input.fill(self.email)
        await self._click_submit(page)

        await self._switch_to_password_if_offered(page)
        await self._raise_for_login_failure_or_challenge(page)

        password_input = page.locator("input[name='passwd'], input[type='password']").first
        try:
            await password_input.wait_for(timeout=20_000)
        except PlaywrightTimeoutError:
            await self._switch_to_password_if_offered(page)
            await self._raise_for_login_failure_or_challenge(page)
            await password_input.wait_for(timeout=5_000)
        if not _is_trusted_microsoft_login_url(page.url):
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Outlook перенаправил ввод пароля на недоверенный адрес.",
            )
        await password_input.fill(self._password)
        await self._click_submit(page)

        await self._handle_stay_signed_in(page)
        await self._wait_for_mailbox(page)

    async def _start_login_from_landing(self, page: Page) -> None:
        """Outlook's public landing page does not always redirect to login.live.com."""

        email_input = page.locator("input[name='loginfmt'], input[type='email']").first
        if not await email_input.is_visible():
            # `nlp=1` is Outlook's own explicit sign-in entrypoint.  It avoids
            # ambiguous Microsoft marketing-page "Sign in" links that can
            # lead to the generic microsoft.com account page.
            await page.goto(
                _OUTLOOK_LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=self._navigation_timeout_ms,
            )

        deadline = time.monotonic() + 12.0
        selectors = (
            "a[data-task='signin']",
            "a[href*='login.live.com']",
            "a[href*='outlook.live.com/owa'][href*='login']",
        )
        while time.monotonic() < deadline:
            if await email_input.is_visible():
                return
            for selector in selectors:
                sign_in = page.locator(selector).first
                if await sign_in.is_visible():
                    await sign_in.click()
                    try:
                        await page.wait_for_url("**login.live.com/**", timeout=15_000)
                    except PlaywrightTimeoutError:
                        # The email form is the source of truth; Microsoft can
                        # use another account subdomain for the same login UI.
                        pass
                    return
            await asyncio.sleep(0.25)

    async def _switch_to_password_if_offered(self, page: Page) -> None:
        deadline = time.monotonic() + 10.0
        password_input = page.locator(
            "input[name='passwd'], input[type='password']"
        ).first
        selectors = (
            "#idA_PWD_SwitchToPassword",
            "a[data-bind*='switchToPassword']",
            "button:has-text('Use your password')",
            "button:has-text('Используйте свой пароль')",
            "a:has-text('Use your password')",
            "a:has-text('Используйте свой пароль')",
            "[role='button']:has-text('Use your password')",
            "[role='button']:has-text('Используйте свой пароль')",
        )
        while time.monotonic() < deadline:
            if await password_input.is_visible():
                return
            for selector in selectors:
                switch = page.locator(selector).first
                if await switch.is_visible():
                    await switch.click()
                    # The Fluent UI control is a span with role=button and
                    # updates the same document asynchronously. Do not inspect
                    # the old challenge copy before the password form appears.
                    try:
                        await password_input.wait_for(
                            state="visible",
                            timeout=max(1, int((deadline - time.monotonic()) * 1000)),
                        )
                    except PlaywrightTimeoutError:
                        await self._raise_for_login_failure_or_challenge(page)
                        return
                    return
            await asyncio.sleep(0.25)
        await self._raise_for_login_failure_or_challenge(page)

    async def _click_submit(self, page: Page) -> None:
        submit = page.locator(
            "#idSIButton9, input[type='submit'], button[type='submit']"
        ).first
        await submit.click(timeout=10_000)

    async def _handle_stay_signed_in(self, page: Page) -> None:
        deadline = time.monotonic() + 12.0
        password_proof_used = False
        while time.monotonic() < deadline:
            if await self._mailbox_is_ready(page):
                return
            if not password_proof_used and await self._submit_password_proof_if_offered(page):
                password_proof_used = True
                await asyncio.sleep(0.5)
                continue
            text = await self._safe_body_text(page)
            if any(
                marker in text
                for marker in (
                    "stay signed in",
                    "не выходить из системы",
                    "оставаться в системе",
                )
            ):
                no_button = page.locator(
                    "#declineButton, #idBtn_Back, "
                    "input[value='No'], input[value='Нет'], "
                    "button:has-text('No'), button:has-text('Нет')"
                ).first
                await no_button.click(timeout=5_000)
                return
            await self._raise_for_login_failure_or_challenge(page)
            await asyncio.sleep(0.25)

    async def _submit_password_proof_if_offered(self, page: Page) -> bool:
        """Complete Microsoft's one-time post-login password proof.

        Consumer accounts can ask to confirm a recovery address while still
        offering the already configured account password as an alternative.
        Fluent UI renders that action as a ``span[role=button]``. The proof is
        attempted once and only on Microsoft's exact HTTPS login hosts.
        """

        selector = (
            "[role='button']:has-text('Use your password'), "
            "[role='button']:has-text('Используйте свой пароль')"
        )
        switch = page.locator(selector).first
        if not await switch.is_visible():
            return False
        if not _is_trusted_microsoft_login_url(page.url):
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Outlook перенаправил ввод пароля на недоверенный адрес.",
            )
        await switch.click(force=True)
        password_input = page.locator(
            "input[name='passwd'], input[type='password']"
        ).first
        await password_input.wait_for(state="visible", timeout=10_000)
        if not _is_trusted_microsoft_login_url(page.url):
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Outlook перенаправил ввод пароля на недоверенный адрес.",
            )
        await password_input.fill(self._password)
        await self._click_submit(page)
        return True

    async def _wait_for_mailbox(self, page: Page) -> None:
        deadline = time.monotonic() + self._navigation_timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self._mailbox_is_ready(page):
                return
            await self._raise_for_login_failure_or_challenge(page)
            await asyncio.sleep(0.5)
        raise EmailProviderError(
            EmailErrorCode.TIMEOUT,
            "Outlook Web не открыл список писем за отведённое время.",
        )

    async def _mailbox_is_ready(self, page: Page) -> bool:
        url = (page.url or "").lower()
        if "outlook.live.com" not in url or not any(
            part in url for part in ("/mail", "/owa")
        ):
            return False
        # Outlook is an SPA; the URL can switch before the mailbox exists.  A
        # generic role=main is also present on Microsoft's public landing, so
        # require the mailbox search plus either its Focused/Other tabs or a
        # real message-list surface.
        search = page.get_by_role(
            "combobox",
            name=re.compile(
                r"search (mail|emails|messages|meetings|files)|"
                r"поиск (писем|сообщений|почты|собраний|файлов)",
                re.IGNORECASE,
            ),
        ).first
        if not await search.is_visible():
            return False

        tabs = page.get_by_role(
            "tab",
            name=re.compile(
                r"focused|other|отсортированные|приоритетные|другие",
                re.IGNORECASE,
            ),
        ).first
        message_list = page.locator(
            "[aria-label*='Message list'], [aria-label*='Список сообщений'], "
            "[data-app-section='Mail'], [role='main'] [role='listbox']"
        ).first
        return await tabs.is_visible() or await message_list.is_visible()

    async def _raise_for_login_failure_or_challenge(self, page: Page) -> None:
        text = await self._safe_body_text(page)
        auth_markers = (
            "your account or password is incorrect",
            "incorrect password",
            "wrong password",
            "пароль неверен",
            "неправильный пароль",
            "эта учетная запись или пароль неверны",
        )
        if any(marker in text for marker in auth_markers):
            raise EmailProviderError(
                EmailErrorCode.AUTH_FAILED,
                "Outlook отклонил адрес почты или пароль.",
            )

        blocking_challenge_markers = (
            "prove you're human",
            "help us beat the robots",
            "unusual activity",
            "подтвердите, что вы человек",
            "необычная активность",
            "слишком много попыток",
            "just a moment",
            "verify you are human",
            "checking your browser",
            "проверка безопасности",
        )
        verification_markers = (
            "verify your identity",
            "help us protect your account",
            "подтвердите свою личность",
            "помогите нам защитить вашу учетную запись",
            "send a code",
            "отправить код",
        )
        challenge_frame = page.locator(
            "iframe[src*='captcha'], iframe[title*='captcha'], [id*='captcha'], "
            "iframe[src*='challenges.cloudflare.com'], #challenge-running, .cf-challenge"
        ).first
        send_code_action = page.locator(
            "button:has-text('Send code'), button:has-text('Отправить код'), "
            "input[value='Send code'], input[value='Отправить код']"
        ).first
        password_input = page.locator(
            "input[name='passwd'], input[type='password']"
        ).first
        password_visible = await password_input.is_visible()
        if (
            any(marker in text for marker in blocking_challenge_markers)
            or await challenge_frame.is_visible()
            or (
                not password_visible
                and (
                    any(marker in text for marker in verification_markers)
                    or await send_code_action.is_visible()
                )
            )
        ):
            raise EmailProviderError(
                EmailErrorCode.SECURITY_CHALLENGE,
                "Outlook потребовал ручную проверку безопасности.",
            )

    async def _scan_all_folders(self, page: Page) -> list[_MessageSnapshot]:
        snapshots: list[_MessageSnapshot] = []
        await self._open_mail_folder(page, _INBOX_FOLDER_PATTERN)
        snapshots.extend(await self._scan_inbox_stably(page))

        junk_opened = await self._open_mail_folder(page, _JUNK_FOLDER_PATTERN)
        if junk_opened:
            snapshots.extend(
                await self._visible_openai_messages(page, folder="junk")
            )
            # Polling must start every pass from Inbox; otherwise a second
            # scan would inspect Junk twice and silently stop checking Inbox.
            await self._open_mail_folder(page, _INBOX_FOLDER_PATTERN)

        # The same DOM row can be visible through nested selectors; keep one.
        return list({snapshot.key: snapshot for snapshot in snapshots}.values())

    async def _scan_inbox_stably(self, page: Page) -> list[_MessageSnapshot]:
        tab_patterns = (
            re.compile(r"other|другие", re.IGNORECASE),
            re.compile(r"focused|отсортированные|приоритетные", re.IGNORECASE),
        )
        for attempt in range(4):
            visible_tabs = await self._visible_inbox_tabs(page, tab_patterns)
            if len(visible_tabs) == 1:
                await asyncio.sleep(0.35)
                continue

            snapshots: list[_MessageSnapshot] = []
            if visible_tabs:
                for pattern in visible_tabs:
                    await self._activate_inbox_tab(page, pattern)
                    snapshots.extend(
                        await self._visible_openai_messages(
                            page,
                            folder="inbox",
                            tab_pattern=pattern.pattern,
                        )
                    )
            else:
                snapshots.extend(
                    await self._visible_openai_messages(page, folder="inbox")
                )

            after = await self._visible_inbox_tabs(page, tab_patterns)
            before_signature = tuple(item.pattern for item in visible_tabs)
            after_signature = tuple(item.pattern for item in after)
            if before_signature == after_signature and len(after) != 1:
                return snapshots
            if attempt < 3:
                await asyncio.sleep(0.35)

        raise EmailProviderError(
            EmailErrorCode.TIMEOUT,
            "Outlook Web не завершил стабильную загрузку вкладок входящих.",
        )

    async def _visible_inbox_tabs(
        self,
        page: Page,
        patterns: tuple[re.Pattern, ...],
    ) -> list[re.Pattern]:
        visible: list[re.Pattern] = []
        for pattern in patterns:
            tab = page.get_by_role("tab", name=pattern).first
            if await tab.is_visible():
                visible.append(pattern)
        return visible

    async def _activate_inbox_tab(
        self,
        page: Page,
        pattern: re.Pattern,
    ) -> None:
        """Select one Focused Inbox tab without accepting a partial scan."""

        for attempt in range(3):
            # Reacquire the locator on every attempt because Outlook replaces
            # the tab node during its own render cycle.
            tab = page.get_by_role("tab", name=pattern).first
            if not await tab.is_visible():
                await asyncio.sleep(0.2)
                continue
            if (await tab.get_attribute("aria-selected") or "").lower() == "true":
                return
            try:
                await tab.click(timeout=3_000)
            except PlaywrightTimeoutError:
                pass
            await asyncio.sleep(0.35)
            selected = page.get_by_role("tab", name=pattern).first
            if (
                await selected.is_visible()
                and (await selected.get_attribute("aria-selected") or "").lower()
                == "true"
            ):
                return

            # Outlook's virtualized tab can remain visible while its pointer
            # action is intercepted by a re-render. Keyboard activation uses
            # the same accessible control and is reliable in that state.
            selected = page.get_by_role("tab", name=pattern).first
            try:
                if await selected.is_visible():
                    if (
                        await selected.get_attribute("aria-selected") or ""
                    ).lower() == "true":
                        return
                    await selected.press("Enter", timeout=3_000)
            except PlaywrightTimeoutError:
                pass
            await asyncio.sleep(0.35)
            selected = page.get_by_role("tab", name=pattern).first
            if (
                await selected.is_visible()
                and (await selected.get_attribute("aria-selected") or "").lower()
                == "true"
            ):
                return
            if attempt < 2:
                await asyncio.sleep(0.2)

        raise EmailProviderError(
            EmailErrorCode.TIMEOUT,
            "Outlook Web не позволил безопасно переключить вкладку входящих.",
        )

    async def _open_mail_folder(self, page: Page, name_pattern: re.Pattern) -> bool:
        """Open a folder and prove that Outlook selected the requested item."""
        for role in ("treeitem", "link", "button"):
            for attempt in range(3):
                # Outlook replaces navigation nodes during SPA updates, so the
                # locator must be reacquired before every action and check.
                item = page.get_by_role(role, name=name_pattern).first
                if not await item.is_visible():
                    break
                if (
                    await item.get_attribute("aria-selected") or ""
                ).lower() == "true":
                    return True
                try:
                    await item.click(timeout=3_000)
                except PlaywrightTimeoutError:
                    pass
                await asyncio.sleep(0.35)
                selected = page.get_by_role(role, name=name_pattern).first
                if (
                    await selected.is_visible()
                    and (
                        await selected.get_attribute("aria-selected") or ""
                    ).lower()
                    == "true"
                ):
                    return True

                # The accessible tree item also supports keyboard activation;
                # this survives pointer interception during Outlook re-renders.
                selected = page.get_by_role(role, name=name_pattern).first
                try:
                    if await selected.is_visible():
                        await selected.press("Enter", timeout=3_000)
                except PlaywrightTimeoutError:
                    pass
                await asyncio.sleep(0.35)
                selected = page.get_by_role(role, name=name_pattern).first
                if (
                    await selected.is_visible()
                    and (
                        await selected.get_attribute("aria-selected") or ""
                    ).lower()
                    == "true"
                ):
                    return True
                if attempt < 2:
                    await asyncio.sleep(0.2)
        return False

    async def _visible_openai_messages(
        self,
        page: Page,
        *,
        folder: str = "inbox",
        tab_pattern: str | None = None,
    ) -> list[_MessageSnapshot]:
        payloads: list[dict[str, Any]] = await page.evaluate(
            """(selectors) => {
                for (const selector of selectors) {
                    const all = Array.from(document.querySelectorAll(selector));
                    const visible = all
                        .map((element, domIndex) => ({element, domIndex}))
                        .filter(({element}) => {
                            const style = window.getComputedStyle(element);
                            return element.getClientRects().length > 0 &&
                                style.visibility !== 'hidden' && style.display !== 'none';
                        });
                    if (!visible.length) continue;
                    return visible.slice(0, 150).map(({element, domIndex}) => ({
                        selector,
                        dom_index: domIndex,
                        text: element.innerText || element.textContent || '',
                        item_id: element.getAttribute('data-item-id') ||
                            element.getAttribute('data-itemid') || '',
                        conversation_id: element.getAttribute('data-convid') ||
                            element.getAttribute('data-conversation-id') || '',
                        element_id: element.id || '',
                        datetime: Array.from(element.querySelectorAll('time'))
                            .map(node => node.getAttribute('datetime') || node.textContent || '')
                            .join('|'),
                    }));
                }
                return [];
            }""",
            list(_ROW_SELECTOR_CANDIDATES),
        )

        collector = self._find_item_collectors.get(id(page))
        if collector is not None:
            await collector.drain()

        snapshots: list[_MessageSnapshot] = []
        for payload in payloads:
            text = str(payload.get("text") or "")
            if not _looks_like_openai_code_candidate(text):
                continue
            item_id = _bounded_outlook_id(payload.get("item_id"))
            conversation_id = _bounded_outlook_id(payload.get("conversation_id"))
            api_state, api_metadata = (
                collector.lookup(
                    item_id=payload.get("item_id"),
                    conversation_id=payload.get("conversation_id"),
                )
                if collector is not None
                else ("missing", None)
            )
            dom_received_at = _received_at(payload)
            api_metadata_trusted = (
                api_state == "trusted"
                and api_metadata is not None
                and api_metadata.received_at is not None
            )
            api_metadata_rejected = api_state == "rejected"
            if api_metadata_trusted:
                if (
                    dom_received_at is not None
                    and dom_received_at != api_metadata.received_at
                ):
                    received_at = None
                    api_metadata_trusted = False
                    api_metadata_rejected = True
                else:
                    received_at = api_metadata.received_at
            elif api_metadata_rejected:
                received_at = None
            else:
                received_at = dom_received_at
            selector = str(payload.get("selector"))
            dom_index = int(payload.get("dom_index", 0))
            snapshots.append(
                _MessageSnapshot(
                    key=_message_fingerprint(payload),
                    text=text,
                    locator=page.locator(selector).nth(dom_index),
                    received_at=received_at,
                    fingerprint=(
                        _fresh_message_fingerprint(payload, received_at)
                        if received_at is not None
                        else None
                    ),
                    folder=folder,
                    tab_pattern=tab_pattern,
                    api_metadata_trusted=api_metadata_trusted,
                    api_metadata_rejected=api_metadata_rejected,
                    item_id=(
                        api_metadata.item_id
                        if api_metadata_trusted and api_metadata is not None
                        else item_id
                    ),
                    conversation_id=(
                        api_metadata.conversation_id
                        if api_metadata_trusted and api_metadata is not None
                        else conversation_id
                    ),
                    api_sender_address=(
                        api_metadata.sender_address
                        if api_metadata_trusted and api_metadata is not None
                        else None
                    ),
                )
            )
        return snapshots

    async def _restore_snapshot(
        self,
        page: Page,
        snapshot: _MessageSnapshot,
    ) -> _MessageSnapshot | None:
        """Reopen the captured view and bind the action to the same row.

        Playwright locators are lazy.  Retaining an ``nth()`` locator while
        moving between Inbox tabs and Junk can otherwise make the later click
        target a different row in the final view.
        """

        folder_pattern = (
            _JUNK_FOLDER_PATTERN if snapshot.folder == "junk" else _INBOX_FOLDER_PATTERN
        )
        if not await self._open_mail_folder(page, folder_pattern):
            return None

        if snapshot.tab_pattern is not None:
            pattern = re.compile(snapshot.tab_pattern, re.IGNORECASE)
            try:
                await self._activate_inbox_tab(page, pattern)
            except EmailProviderError:
                return None

        current = await self._visible_openai_messages(
            page,
            folder=snapshot.folder,
            tab_pattern=snapshot.tab_pattern,
        )
        return next((item for item in current if item.key == snapshot.key), None)

    async def _read_code(
        self,
        page: Page,
        snapshot: _MessageSnapshot,
        *,
        not_before: datetime | None = None,
    ) -> str | None:
        self._last_read_evidence = None
        current = await self._restore_snapshot(page, snapshot)
        if current is None:
            return None
        if current.api_metadata_rejected:
            return None
        collector = self._find_item_collectors.get(id(page))
        if collector is None or current.conversation_id is None:
            return None
        checkpoint = await collector.checkpoint_opened_message()
        await current.locator.click(timeout=10_000)
        await asyncio.sleep(0.5)

        # The Outlook list may expose only an attacker-controlled display
        # name. Correlate the click with exactly one fresh same-origin API
        # response and validate its structured identity before returning a
        # code from that same record.
        opened_state, opened_metadata = await collector.opened_message_after(
            checkpoint,
            conversation_id=current.conversation_id,
            item_id=current.item_id,
            received_at=current.received_at,
            not_before=not_before,
        )
        if (
            opened_state != "trusted"
            or opened_metadata is None
            or opened_metadata.metadata.item_id is None
            or opened_metadata.metadata.received_at is None
            or opened_metadata.metadata.sender_address not in _OPENAI_SENDER_ADDRESSES
            or opened_metadata.verification_code is None
        ):
            return None
        exact_metadata = opened_metadata.metadata
        if (
            current.received_at is not None
            and current.received_at != exact_metadata.received_at
        ):
            return None
        if not_before is not None:
            cutoff = (
                not_before.replace(tzinfo=UTC)
                if not_before.tzinfo is None
                else not_before.astimezone(UTC)
            )
            received_at = exact_metadata.received_at
            if received_at is None or received_at < cutoff:
                return None

        # The code was extracted transiently from the exact bounded
        # GetConversationItems record. Never read or merge DOM conversation
        # bodies, which may still show an older message after the click.
        code = opened_metadata.verification_code
        fingerprint = _fresh_message_fingerprint(
            {
                "item_id": exact_metadata.item_id,
                "conversation_id": exact_metadata.conversation_id,
            },
            exact_metadata.received_at,
        )
        if fingerprint is None:
            return None
        self._last_read_evidence = (exact_metadata.received_at, fingerprint)
        return code

    @staticmethod
    async def _opened_message_received_at(page: Page) -> datetime | None:
        """Read one unambiguous timezone-aware date from trusted header UI.

        Outlook sometimes omits ``time[datetime]`` from virtualized list rows.
        The active read surface can still expose the received timestamp.  Only
        visible metadata outside all message-body subtrees is considered; the
        body is sender-controlled and must never be allowed to prove freshness.
        """

        for attempt in range(3):
            values: object = await page.evaluate(
                """() => {
                    const isVisible = (element) => {
                        const style = window.getComputedStyle(element);
                        return element.getClientRects().length > 0 &&
                            style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const readSurfaces = Array.from(document.querySelectorAll(
                        "[data-app-section='MailReadCompose']"
                    )).filter(isVisible);
                    if (readSurfaces.length !== 1) return [];
                    const root = readSurfaces[0];
                    const bodySelector = [
                        "[role='document']",
                        "[aria-label*='Message body']",
                        "[aria-label*='Тело сообщения']"
                    ].join(", ");
                    const bodies = Array.from(root.querySelectorAll(bodySelector));
                    if (bodies.filter(isVisible).length !== 1) return [];
                    const values = [];
                    for (const element of root.querySelectorAll("time[datetime]")) {
                        if (!isVisible(element)) continue;
                        if (bodies.some(
                            (body) => body === element || body.contains(element)
                        )) {
                            continue;
                        }
                        const value = element.getAttribute("datetime");
                        if (value) values.push(value);
                    }
                    return values.slice(0, 20);
                }"""
            )
            parsed: set[datetime] = set()
            if isinstance(values, list):
                for value in values:
                    if not isinstance(value, str):
                        continue
                    received_at = _received_at({"datetime": value})
                    if received_at is not None:
                        parsed.add(received_at)
            if len(parsed) == 1:
                return next(iter(parsed))
            if len(parsed) > 1:
                return None
            if attempt < 2:
                await asyncio.sleep(0.25)
        return None

    @staticmethod
    async def _has_trusted_openai_sender_header(
        page: Page,
        *,
        expected_sender: str | None = None,
    ) -> bool:
        """Verify the sender from opened-message metadata, never body text.

        Outlook's generated class names are unstable, while sender controls
        expose semantic attributes such as ``aria-label``, ``title`` and
        ``data-email-address``.  Read those attributes only from the active
        mail-reading surface and exclude every message-body subtree.  No link
        is followed and no security notification is acted upon.
        """

        evidence: object = await page.evaluate(
            """() => {
                const isVisible = (element) => {
                    const style = window.getComputedStyle(element);
                    return element.getClientRects().length > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const readSurfaces = Array.from(document.querySelectorAll(
                    "[data-app-section='MailReadCompose']"
                )).filter(isVisible);
                if (readSurfaces.length !== 1) return [];
                const root = readSurfaces[0];
                const bodySelector = [
                    "[role='document']",
                    "[aria-label*='Message body']",
                    "[aria-label*='Тело сообщения']"
                ].join(", ");
                const bodies = Array.from(root.querySelectorAll(bodySelector));
                const visibleBodies = bodies.filter(isVisible);
                if (visibleBodies.length !== 1) {
                    return {values: [], sender_values: [], from_bound: false};
                }
                const metadataSelector = [
                    "[aria-label*='@']",
                    "[title*='@']",
                    "[data-email-address]",
                    "[data-email]",
                    "[data-address]",
                    "a[href^='mailto:']"
                ].join(", ");
                const attributes = [
                    "aria-label",
                    "title",
                    "data-email-address",
                    "data-email",
                    "data-address",
                    "href"
                ];
                const values = [];
                for (const element of root.querySelectorAll(metadataSelector)) {
                    if (!isVisible(element)) continue;
                    if (bodies.some(
                        (body) => body === element || body.contains(element)
                    )) {
                        continue;
                    }
                    for (const attribute of attributes) {
                        const value = element.getAttribute(attribute);
                        if (value) values.push(value);
                    }
                    if (values.length >= 100) break;
                }
                const senderRootSelector = [
                    "[data-testid*='sender' i]",
                    "[data-automationid*='sender' i]",
                    "[data-app-section*='sender' i]",
                    "[data-sender-address]",
                    "[data-sender-email]",
                    "[aria-label^='From:' i]",
                    "[aria-label^='From ' i]",
                    "[aria-label^='Sender:' i]",
                    "[aria-label^='От:' i]",
                    "[aria-label^='От ' i]",
                    "[aria-label^='Отправитель' i]",
                    "[title^='From:' i]",
                    "[title^='Sender:' i]",
                    "[title^='От:' i]",
                    "[title^='Отправитель' i]"
                ].join(", ");
                const senderValues = [];
                const senderRoots = Array.from(
                    root.querySelectorAll(senderRootSelector)
                ).filter((element) => isVisible(element) && !bodies.some(
                    (body) => body === element || body.contains(element)
                ));
                for (const senderRoot of senderRoots) {
                    const candidates = [
                        senderRoot,
                        ...senderRoot.querySelectorAll(metadataSelector)
                    ];
                    for (const element of candidates) {
                        if (!isVisible(element)) continue;
                        for (const attribute of attributes) {
                            const value = element.getAttribute(attribute);
                            if (value) senderValues.push(value);
                        }
                    }
                }
                return {
                    values,
                    sender_values: senderValues.slice(0, 100),
                    from_bound: senderRoots.length > 0
                };
            }"""
        )
        # Lists are accepted only for the small pure unit-test doubles used by
        # this module. Real browser execution always returns the object above.
        if isinstance(evidence, list):
            values = tuple(value for value in evidence if isinstance(value, str))
            from_bound = True
        elif isinstance(evidence, dict):
            raw_values = (
                evidence.get("sender_values")
                if evidence.get("from_bound")
                else evidence.get("values")
            )
            if not isinstance(raw_values, list):
                return False
            values = tuple(value for value in raw_values if isinstance(value, str))
            from_bound = bool(evidence.get("from_bound"))
        else:
            return False
        if expected_sender is None and not from_bound:
            return False
        addresses = _exact_header_addresses(values)
        if addresses is None or len(addresses) != 1:
            return False
        sender = next(iter(addresses))
        if sender not in _OPENAI_SENDER_ADDRESSES:
            return False
        return expected_sender is None or sender == expected_sender.casefold()

    @staticmethod
    async def _safe_body_text(page: Page) -> str:
        try:
            return _normalise_text(await page.locator("body").inner_text(timeout=1_000))
        except Exception:
            return ""
