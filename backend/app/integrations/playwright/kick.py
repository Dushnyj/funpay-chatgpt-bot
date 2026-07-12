import base64
import secrets

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from app.integrations.email.provider import EmailProvider
from app.integrations.openai.oauth import OPENAI_ISSUER
from app.integrations.playwright.oauth_login import (
    _build_authorize_url,
    _do_post_password_steps,
    _generate_pkce,
)
from app.services.totp import generate_totp

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
    """Проходит OAuth-логин через тот же authorize URL, что и первичная валидация.

    Поддержка подтверждений после пароля через общий хелпер `_do_post_password_steps`:
      - email_provider задан → обрабатывает email-code.
      - totp_secret задан → обрабатывает 2FA TOTP.
    Обратно совместим: вызов `_login(page, login, password, totp_secret, timeout_ms)` работает.
    """
    verifier, challenge = _generate_pkce()
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    auth_url = _build_authorize_url(challenge, state)

    await page.goto(auth_url, wait_until="networkidle", timeout=timeout_ms)

    email_input = page.locator('input[name="email"], input[type="email"]').first
    await email_input.fill(login)
    await page.get_by_role("button", name="Continue").click()

    password_input = page.locator('input[name="password"], input[type="password"]').first
    await password_input.fill(password)
    await page.get_by_role("button", name="Continue").click()

    # email-code и/или 2FA (общий хелпер из oauth_login).
    await _do_post_password_steps(page, totp_secret, email_provider)

    # Ждём перехода в ChatGPT — значит логин успешен
    await page.wait_for_url("**/chatgpt.com/**", timeout=timeout_ms)


async def _logout_everywhere(page: Page, timeout_ms: int) -> None:
    """Открывает настройки и нажимает 'Log out of all sessions' с фолбэками по локали."""
    await page.goto(_SETTINGS_URL, wait_until="networkidle", timeout=timeout_ms)

    # Кнопка может называться по-разному в зависимости от локали — пробуем каноничный вариант
    logout_btn = page.get_by_role("button", name="Log out of all sessions")
    try:
        await logout_btn.click(timeout=10_000)
    except PlaywrightTimeoutError:
        # Fallback: ищем по тексту (en/ru варианты)
        alt_btn = page.locator(
            "button:has-text('all devices'), button:has-text('всех устройствах')"
        ).first
        await alt_btn.click(timeout=10_000)

    # Подтверждение, если есть диалог
    try:
        confirm = page.get_by_role("button", name="Confirm, Log out, ОК").first
        await confirm.click(timeout=5_000)
    except PlaywrightTimeoutError:
        pass  # подтверждения не было
