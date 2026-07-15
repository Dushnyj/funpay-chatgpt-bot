from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.integrations.email.provider import (
    EmailErrorCode,
    EmailProviderError,
    FreshVerificationCode,
    parse_verification_code,
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


@dataclass(frozen=True, slots=True)
class _MessageSnapshot:
    key: str
    text: str
    locator: Locator
    received_at: datetime | None = None
    fingerprint: str | None = None
    folder: str = "inbox"
    tab_pattern: str | None = None


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
    for candidate in str(payload.get("datetime") or "").split("|"):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            value = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is not None:
            return value.astimezone(UTC)
    return None


def _fresh_message_fingerprint(
    payload: dict[str, Any],
    received_at: datetime,
) -> str | None:
    # DOM element ids may change between mailbox sessions. At least one
    # Outlook item/conversation id is required for durable per-rental dedupe.
    stable_ids = "|".join(
        str(payload.get(name) or "")
        for name in ("item_id", "conversation_id")
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
    ) -> None:
        self.email = email
        self._password = password
        self._headless = headless
        self._navigation_timeout_ms = navigation_timeout_ms
        self._poll_interval_s = poll_interval_s
        self._baseline_keys: set[str] = set()
        self._baseline_at: datetime | None = None
        self._storage_state: dict[str, Any] | None = None
        self._preflight_complete = False

    @property
    def baseline_at(self) -> datetime | None:
        """Timestamp of the last in-memory baseline (never persisted)."""

        return self._baseline_at

    async def preflight(self) -> None:
        """Log in and remember every currently visible OpenAI code message."""

        try:
            async with self._mailbox_session() as (page, context):
                snapshots = await self._scan_all_folders(page)
                self._baseline_keys = {snapshot.key for snapshot in snapshots}
                self._baseline_at = datetime.now(UTC)
                self._storage_state = await context.storage_state()
                self._preflight_complete = True
        except EmailProviderError:
            raise
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            raise EmailProviderError(
                EmailErrorCode.TIMEOUT,
                "Outlook Web не ответил за отведённое время.",
            ) from exc
        except Exception as exc:
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
                        code = await self._read_code(page, snapshot)
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
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            raise EmailProviderError(
                EmailErrorCode.TIMEOUT,
                "Outlook Web не ответил при ожидании письма.",
            ) from exc
        except Exception as exc:
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
                            snapshot.received_at is None
                            or snapshot.fingerprint is None
                            or snapshot.received_at < cutoff
                        ):
                            continue
                        code = await self._read_code(page, snapshot)
                        if code is not None:
                            return FreshVerificationCode(
                                code=code,
                                received_at=snapshot.received_at,
                                fingerprint=snapshot.fingerprint,
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
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            raise EmailProviderError(
                EmailErrorCode.TIMEOUT,
                "Outlook Web не ответил при чтении свежего письма.",
            ) from exc
        except Exception as exc:
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
            )
            context = await browser.new_context(
                storage_state=self._storage_state,
                locale="ru-RU",
            )
            page = await context.new_page()
            page.set_default_timeout(self._navigation_timeout_ms)
            try:
                await self._open_mailbox(page)
                yield page, context
            finally:
                await context.close()
                await browser.close()

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
        visited_tab = False
        for pattern in (
            re.compile(r"other|другие", re.IGNORECASE),
            re.compile(r"focused|отсортированные|приоритетные", re.IGNORECASE),
        ):
            tab = page.get_by_role("tab", name=pattern).first
            if not await tab.is_visible():
                continue
            visited_tab = True
            await tab.click()
            await asyncio.sleep(0.35)
            snapshots.extend(
                await self._visible_openai_messages(
                    page,
                    folder="inbox",
                    tab_pattern=pattern.pattern,
                )
            )

        if not visited_tab:
            snapshots.extend(
                await self._visible_openai_messages(page, folder="inbox")
            )

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

    async def _open_mail_folder(self, page: Page, name_pattern: re.Pattern) -> bool:
        """Open an Outlook folder through its accessible navigation item."""
        for role in ("treeitem", "link", "button"):
            item = page.get_by_role(role, name=name_pattern).first
            try:
                if not await item.is_visible():
                    continue
                await item.click(timeout=5_000)
                await asyncio.sleep(0.35)
                return True
            except PlaywrightTimeoutError:
                continue
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

        snapshots: list[_MessageSnapshot] = []
        for payload in payloads:
            text = str(payload.get("text") or "")
            if not _looks_like_openai_code_candidate(text):
                continue
            selector = str(payload.get("selector"))
            dom_index = int(payload.get("dom_index", 0))
            snapshots.append(
                _MessageSnapshot(
                    key=_message_fingerprint(payload),
                    text=text,
                    locator=page.locator(selector).nth(dom_index),
                    received_at=(received_at := _received_at(payload)),
                    fingerprint=(
                        _fresh_message_fingerprint(payload, received_at)
                        if received_at is not None
                        else None
                    ),
                    folder=folder,
                    tab_pattern=tab_pattern,
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
            tab = page.get_by_role(
                "tab",
                name=re.compile(snapshot.tab_pattern, re.IGNORECASE),
            ).first
            if not await tab.is_visible():
                return None
            await tab.click()
            await asyncio.sleep(0.35)

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
    ) -> str | None:
        current = await self._restore_snapshot(page, snapshot)
        if current is None:
            return None
        await current.locator.click(timeout=10_000)
        await asyncio.sleep(0.5)

        # The Outlook list may expose only the attacker-controlled display
        # name.  After opening the candidate, trust it only when Outlook's
        # message-header metadata contains an exact allow-listed sender
        # address.  Values inside the message body are deliberately excluded.
        if not await self._has_trusted_openai_sender_header(page):
            return None

        body_parts: list[str] = []
        for selector in (
            "[role='document']",
            "[aria-label*='Message body']",
            "[aria-label*='Тело сообщения']",
            "[data-app-section='MailReadCompose']",
        ):
            locator = page.locator(selector)
            if await locator.count():
                body_parts.extend(await locator.all_inner_texts())

        combined = "\n".join((current.text, *body_parts))
        if not _has_openai_code_subject(combined):
            return None
        return parse_verification_code(combined)

    @staticmethod
    async def _has_trusted_openai_sender_header(page: Page) -> bool:
        """Verify the sender from opened-message metadata, never body text.

        Outlook's generated class names are unstable, while sender controls
        expose semantic attributes such as ``aria-label``, ``title`` and
        ``data-email-address``.  Read those attributes only from the active
        mail-reading surface and exclude every message-body subtree.  No link
        is followed and no security notification is acted upon.
        """

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
                const root = readSurfaces[0] || document;
                const bodySelector = [
                    "[role='document']",
                    "[aria-label*='Message body']",
                    "[aria-label*='Тело сообщения']"
                ].join(", ");
                const bodies = Array.from(root.querySelectorAll(bodySelector));
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
                return values;
            }"""
        )
        if not isinstance(values, list):
            return False
        return _has_trusted_openai_sender(
            tuple(value for value in values if isinstance(value, str))
        )

    @staticmethod
    async def _safe_body_text(page: Page) -> str:
        try:
            return _normalise_text(await page.locator("body").inner_text(timeout=1_000))
        except Exception:
            return ""
