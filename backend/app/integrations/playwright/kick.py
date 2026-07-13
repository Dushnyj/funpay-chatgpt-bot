import asyncio
import re

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from app.integrations.email.provider import EmailProvider
from app.integrations.playwright.oauth_login import (
    login_and_get_auth_code,
    raise_if_cloudflare,
)

_SETTINGS_URL = "https://chatgpt.com/#settings"


class KickError(Exception):
    """Сбой кика (логин не удался / страница настроек недоступна)."""


async def kick_account(
    context: BrowserContext,
    login: str,
    password: str,
    totp_secret: str = "",
    timeout_ms: int = 60_000,
    email_provider: EmailProvider | None = None,
) -> None:
    """Логинится в аккаунт и нажимает 'Log out of all sessions'.

    Сбрасывает все сессии (включая арендаторов) — используется при истечении аренды.
    Селекторы OpenAI могут меняться — при сбое уточнять по фактической DOM.

    email_provider/totp_secret опциональны — поддерживают аккаунты с email-code и/или 2FA.
    """
    page = await context.new_page()
    try:
        await _login(page, login, password, totp_secret, timeout_ms, email_provider)
        await _logout_everywhere(page, timeout_ms)
    finally:
        await page.close()


async def _login(
    page: Page,
    login: str,
    password: str,
    totp_secret: str = "",
    timeout_ms: int = 60_000,
    email_provider: EmailProvider | None = None,
) -> None:
    """Establish a ChatGPT web session through the validated OAuth state machine.

    Поддержка подтверждений после пароля через общий хелпер `_do_post_password_steps`:
      - email_provider задан → обрабатывает email-code.
      - totp_secret задан → обрабатывает 2FA TOTP.
    Обратно совместим: вызов `_login(page, login, password, totp_secret, timeout_ms)` работает.
    """
    # The Codex OAuth redirect intentionally goes to localhost, not ChatGPT.
    # Reusing the complete OAuth flow gives us password/email-code/TOTP support
    # and leaves the authenticated OpenAI cookies in this browser context.
    await login_and_get_auth_code(
        page.context,
        login,
        password,
        totp_secret,
        timeout_ms,
        email_provider,
    )
    await page.goto(
        "https://chatgpt.com/",
        wait_until="domcontentloaded",
        timeout=timeout_ms,
    )
    await raise_if_cloudflare(page, "chatgpt_session")
    url = page.url.casefold()
    body = (await page.text_content("body", timeout=2_000) or "").casefold()
    if "/auth/login" in url or "/sign-in" in url or "log in" in body and "sign up" in body:
        raise KickError("OpenAI OAuth completed, but the ChatGPT web session was not created")


async def _logout_everywhere(page: Page, timeout_ms: int) -> None:
    """Открывает настройки и нажимает 'Log out of all sessions' с фолбэками по локали."""
    await page.goto(_SETTINGS_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    await raise_if_cloudflare(page, "logout_settings")

    logout_clicked = False
    for candidate in (
        page.get_by_role(
            "button",
            name=re.compile(
                r"log\s*out.*(?:all sessions|all devices)|"
                r"выйти.*(?:всех сеанс|всех устройств)",
                re.IGNORECASE,
            ),
        ).first,
        page.locator(
            "button:has-text('all sessions'), button:has-text('all devices'), "
            "button:has-text('всех сеанс'), button:has-text('всех устройств')"
        ).first,
    ):
        try:
            await candidate.click(timeout=8_000)
            logout_clicked = True
            break
        except PlaywrightTimeoutError:
            continue
    if not logout_clicked:
        raise KickError("Кнопка выхода со всех устройств не найдена в настройках OpenAI")

    # Подтверждение, если есть диалог. Search inside the dialog so we cannot
    # accidentally click the original settings button for a second time.
    try:
        dialog = page.get_by_role("dialog").last
        confirm = dialog.get_by_role(
            "button",
            name=re.compile(
                r"^(?:confirm|log out|log out all|yes|ok|"
                r"подтвердить|выйти|выйти везде|да|ок)$",
                re.IGNORECASE,
            ),
        ).first
        await confirm.click(timeout=5_000)
    except PlaywrightTimeoutError:
        pass

    # Never report success merely because the clicks did not throw. OpenAI
    # either redirects to auth or shows an explicit success notification.
    deadline = asyncio.get_running_loop().time() + min(15, timeout_ms / 1000)
    success_markers = (
        "logged out of all sessions",
        "logged out on all devices",
        "all sessions have been logged out",
        "successfully logged out",
        "вы вышли на всех устройствах",
        "выход выполнен на всех устройствах",
        "все сеансы завершены",
    )
    while asyncio.get_running_loop().time() < deadline:
        url = page.url.casefold()
        if any(marker in url for marker in ("auth.openai.com", "/auth/login", "/sign-in")):
            return
        try:
            body = (await page.text_content("body", timeout=1_000) or "").casefold()
        except PlaywrightTimeoutError:
            body = ""
        if any(marker in body for marker in success_markers):
            return
        await asyncio.sleep(0.25)
    raise KickError("OpenAI не подтвердил выход со всех устройств")
