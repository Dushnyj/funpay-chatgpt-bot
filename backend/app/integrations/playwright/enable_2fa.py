from __future__ import annotations

import logging
import re

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from app.integrations.email.provider import EmailProvider
from app.integrations.email.qr_decode import decode_qr_secret
from app.integrations.playwright.kick import _login
from app.integrations.playwright.oauth_login import raise_if_cloudflare
from app.services.totp import generate_totp

logger = logging.getLogger(__name__)

_SETTINGS_URL = "https://chatgpt.com/#settings"


class Enable2FAError(Exception):
    """Сбой включения 2FA (UI недоступен / QR не декодирован / код отклонён)."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "setup_2fa_failed",
        stage: str = "setup_2fa",
    ) -> None:
        self.code = code
        self.stage = stage
        self.detail = detail
        super().__init__(detail)


async def enable_2fa(
    context: BrowserContext,
    login: str,
    password: str,
    email_provider: EmailProvider,
    timeout_ms: int = 60_000,
) -> str:
    """Включает TOTP-2FA на аккаунте через Playwright и возвращает secret.

    Flow: логин (с email-code поддержкой) → Settings → Security → Enable
    authenticator → QR скриншот → decode_qr_secret → подтвердить первым TOTP.

    Возвращает base32 secret, который нужно сохранить на аккаунте для последующих
    логинов. email_provider обязателен — на enable_2fa flow OpenAI тоже шлёт коды.

    ⚠️ СЕЛЕКТОРЫ ТРЕБУЮТ ОТЛАДКИ НА РЕАЛЬНОМ АККАУНТЕ — UI OpenAI меняется часто.
       Шаблоны названий кнопок основаны на публичной документации и могут отличаться.
    """
    page = await context.new_page()
    try:
        # Логин без TOTP (включаем впервые). email_provider обрабатывает email-code.
        await _login(page, login, password, "", timeout_ms, email_provider)
        await raise_if_cloudflare(page, "setup_2fa_login")
        secret = await _enable_authenticator(page, timeout_ms)
        return secret
    except Enable2FAError:
        raise
    except PlaywrightTimeoutError as exc:
        raise Enable2FAError(
            "Интерфейс OpenAI не ответил за отведённое время.",
            code="setup_2fa_ui_timeout",
            stage="setup_2fa_login",
        ) from exc
    finally:
        await page.close()


async def _enable_authenticator(page: Page, timeout_ms: int) -> str:
    """Навигация к Settings → Security → Enable 2FA, чтение QR, подтверждение кодом.

    ⚠️ СЕЛЕКТОРЫ ТРЕБУЮТ ОТЛАДКИ НА РЕАЛЬНОМ АККАУНТЕ — UI OpenAI меняется часто.
    """
    await page.goto(_SETTINGS_URL, wait_until="networkidle", timeout=timeout_ms)
    await raise_if_cloudflare(page, "setup_2fa_settings")

    # Шаг 1: найти кнопку/ссылку включения 2FA.
    # Возможные варианты текста: "Authenticator app", "Enable two-factor", "Turn on 2FA".
    enabled_2fa = False
    for selector in [
        page.get_by_role("button", name="Enable two-factor authentication"),
        page.get_by_role("button", name="Turn on two-factor authentication"),
        page.locator("button:has-text('authenticator')"),
        page.locator("a:has-text('authenticator')"),
    ]:
        try:
            await selector.first.click(timeout=5_000)
            enabled_2fa = True
            break
        except PlaywrightTimeoutError:
            continue

    if not enabled_2fa:
        raise Enable2FAError(
            "Не найдена кнопка включения 2FA в настройках OpenAI.",
            code="setup_2fa_button_not_found",
            stage="setup_2fa_settings",
        )

    # Шаг 2: дождаться появления QR-кода и сделать скриншот.
    # QR может быть в <img>, <canvas>, или <svg>. Пробуем разные селекторы.
    qr_image = None
    for selector in [
        page.locator("img[src*='data:image']"),
        page.locator("canvas"),
        page.locator("svg"),
        page.locator("img[alt*='QR']"),
    ]:
        try:
            await selector.first.wait_for(timeout=10_000)
            qr_image = selector.first
            break
        except PlaywrightTimeoutError:
            continue

    if qr_image is None:
        raise Enable2FAError(
            "QR-код не найден на странице настройки 2FA.",
            code="setup_2fa_qr_not_found",
            stage="setup_2fa_qr",
        )

    screenshot = await qr_image.screenshot()
    secret = decode_qr_secret(screenshot)
    if secret is None:
        raise Enable2FAError(
            "QR-код 2FA не удалось декодировать.",
            code="setup_2fa_qr_invalid",
            stage="setup_2fa_qr",
        )

    # Шаг 3: ввести первый TOTP-код для подтверждения.
    code = generate_totp(secret)
    code_input = page.locator('input[name="code"], input[inputmode="numeric"]').first
    await code_input.wait_for(timeout=10_000)
    await code_input.fill(code)
    # Кнопка подтверждения может называться Continue / Verify / Confirm.
    await page.get_by_role(
        "button", name=re.compile(r"continue|verify|confirm", re.IGNORECASE)
    ).first.click(timeout=10_000)
    await raise_if_cloudflare(page, "setup_2fa_confirm")

    return secret
