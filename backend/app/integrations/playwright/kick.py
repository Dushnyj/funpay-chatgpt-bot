import base64
import secrets

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from app.integrations.openai.oauth import OPENAI_ISSUER
from app.integrations.playwright.oauth_login import _build_authorize_url, _generate_pkce
from app.services.totp import generate_totp

_SETTINGS_URL = "https://chatgpt.com/#settings"


class KickError(Exception):
    """Сбой кика (логин не удался / страница настроек недоступна)."""


async def kick_account(
    context: BrowserContext,
    login: str,
    password: str,
    totp_secret: str,
    timeout_ms: int = 60_000,
) -> None:
    """Логинится в аккаунт и нажимает 'Log out of all sessions'.

    Сбрасывает все сессии (включая арендаторов) — используется при истечении аренды.
    Селекторы OpenAI могут меняться — при сбое уточнять по фактической DOM.
    """
    page = await context.new_page()
    try:
        await _login(page, login, password, totp_secret, timeout_ms)
        await _logout_everywhere(page, timeout_ms)
    finally:
        await page.close()


async def _login(
    page: Page, login: str, password: str, totp_secret: str, timeout_ms: int
) -> None:
    """Проходит OAuth-логин через тот же authorize URL, что и первичная валидация."""
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

    # 2FA, если требуется
    try:
        otp_input = page.locator('input[name="code"], input[inputmode="numeric"]').first
        await otp_input.wait_for(timeout=15_000)
        await otp_input.fill(generate_totp(totp_secret))
        await page.get_by_role("button", name="Continue").click()
    except PlaywrightTimeoutError:
        pass  # 2FA не потребовалось

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
