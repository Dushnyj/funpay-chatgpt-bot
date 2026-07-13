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
    parse_verification_code,
)

logger = logging.getLogger(__name__)

_OUTLOOK_URL = "https://outlook.live.com/mail/0/"
_OUTLOOK_LOGIN_URL = "https://outlook.live.com/mail/?prompt=select_account"
_OUTLOOK_DOMAINS = frozenset({"outlook.com", "hotmail.com", "live.com", "msn.com"})
_OPENAI_SENDER_MARKERS = (
    "noreply@tm.openai.com",
    "noreply@openai.com",
)
_OPENAI_SUBJECT_MARKERS = (
    "temporary chatgpt code",
    "temporary code",
    "verification code",
    "login code",
    "sign-in code",
    "временный код chatgpt",
    "временный код",
    "код подтверждения",
    "код входа",
)
_ROW_SELECTOR_CANDIDATES = (
    "[data-item-id]",
    "[data-convid]",
    "[data-conversation-id]",
    "[role='option']",
    "[role='row']",
)


@dataclass(frozen=True, slots=True)
class _MessageSnapshot:
    key: str
    text: str
    locator: Locator


def is_outlook_address(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return domain in _OUTLOOK_DOMAINS


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _looks_like_openai_code_message(text: str) -> bool:
    normalised = _normalise_text(text)
    has_transactional_sender = _OPENAI_SENDER_MARKERS[0] in normalised
    has_sender = any(marker in normalised for marker in _OPENAI_SENDER_MARKERS)
    has_openai_name = "openai" in normalised
    has_code_subject = any(marker in normalised for marker in _OPENAI_SUBJECT_MARKERS)
    return has_transactional_sender or (
        has_code_subject and (has_sender or has_openai_name)
    )


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
        selectors = (
            "#idA_PWD_SwitchToPassword",
            "a[data-bind*='switchToPassword']",
            "button:has-text('Use your password')",
            "button:has-text('Используйте свой пароль')",
            "a:has-text('Use your password')",
            "a:has-text('Используйте свой пароль')",
        )
        while time.monotonic() < deadline:
            if await page.locator("input[name='passwd'], input[type='password']").first.is_visible():
                return
            for selector in selectors:
                switch = page.locator(selector).first
                if await switch.is_visible():
                    await switch.click()
                    return
            await self._raise_for_login_failure_or_challenge(page)
            await asyncio.sleep(0.25)

    async def _click_submit(self, page: Page) -> None:
        submit = page.locator(
            "#idSIButton9, input[type='submit'], button[type='submit']"
        ).first
        await submit.click(timeout=10_000)

    async def _handle_stay_signed_in(self, page: Page) -> None:
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if await self._mailbox_is_ready(page):
                return
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

        challenge_markers = (
            "prove you're human",
            "help us beat the robots",
            "unusual activity",
            "verify your identity",
            "help us protect your account",
            "подтвердите, что вы человек",
            "подтвердите свою личность",
            "необычная активность",
            "помогите нам защитить вашу учетную запись",
            "слишком много попыток",
            "send a code",
            "отправить код",
            "just a moment",
            "verify you are human",
            "checking your browser",
            "проверка безопасности",
        )
        challenge_frame = page.locator(
            "iframe[src*='captcha'], iframe[title*='captcha'], [id*='captcha'], "
            "iframe[src*='challenges.cloudflare.com'], #challenge-running, .cf-challenge"
        ).first
        send_code_action = page.locator(
            "button:has-text('Send code'), button:has-text('Отправить код'), "
            "input[value='Send code'], input[value='Отправить код']"
        ).first
        if (
            any(marker in text for marker in challenge_markers)
            or await challenge_frame.is_visible()
            or await send_code_action.is_visible()
        ):
            raise EmailProviderError(
                EmailErrorCode.SECURITY_CHALLENGE,
                "Outlook потребовал ручную проверку безопасности.",
            )

    async def _scan_all_folders(self, page: Page) -> list[_MessageSnapshot]:
        snapshots: list[_MessageSnapshot] = []
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
            snapshots.extend(await self._visible_openai_messages(page))

        if not visited_tab:
            snapshots.extend(await self._visible_openai_messages(page))

        # The same DOM row can be visible through nested selectors; keep one.
        return list({snapshot.key: snapshot for snapshot in snapshots}.values())

    async def _visible_openai_messages(self, page: Page) -> list[_MessageSnapshot]:
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
            if not _looks_like_openai_code_message(text):
                continue
            selector = str(payload.get("selector"))
            dom_index = int(payload.get("dom_index", 0))
            snapshots.append(
                _MessageSnapshot(
                    key=_message_fingerprint(payload),
                    text=text,
                    locator=page.locator(selector).nth(dom_index),
                )
            )
        return snapshots

    async def _read_code(
        self,
        page: Page,
        snapshot: _MessageSnapshot,
    ) -> str | None:
        await snapshot.locator.click(timeout=10_000)
        await asyncio.sleep(0.5)

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

        combined = "\n".join((snapshot.text, *body_parts))
        if not _looks_like_openai_code_message(combined):
            return None
        return parse_verification_code(combined)

    @staticmethod
    async def _safe_body_text(page: Page) -> str:
        try:
            return _normalise_text(await page.locator("body").inner_text(timeout=1_000))
        except Exception:
            return ""
