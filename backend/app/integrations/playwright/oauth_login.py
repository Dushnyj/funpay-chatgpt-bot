import asyncio
import base64
import hashlib
import logging
import secrets
from urllib.parse import urlencode, parse_qs, urlparse

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from app.integrations.email.provider import EmailProvider
from app.integrations.openai.oauth import OPENAI_CLIENT_ID, OPENAI_ISSUER
from app.services.totp import generate_totp

logger = logging.getLogger(__name__)

# URL OAuth-авторизации Codex CLI (реверс-инжиниринг codex-switcher)
_AUTHORIZE_BASE = f"{OPENAI_ISSUER}/oauth/authorize"
_REDIRECT_URI = "http://localhost:1455/auth/callback"
_SCOPE = "openid profile email offline_access"

# Окно ожидания email-code / 2FA input после ввода пароля (сек)
_POST_PASSWORD_CODE_WAIT_S = 5.0


class OAuthLoginError(Exception):
    """Сбой логина через Playwright (неверные данные / бан / таймаут)."""


def _generate_pkce() -> tuple[str, str]:
    """Генерирует PKCE пару: (code_verifier, code_challenge) методом S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _build_authorize_url(code_challenge: str, state: str) -> str:
    """Собирает URL авторизации с PKCE-параметрами Codex CLI flow."""
    params = {
        "response_type": "code",
        "client_id": OPENAI_CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "scope": _SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "codex_cli_rs",
    }
    return f"{_AUTHORIZE_BASE}?{urlencode(params)}"


async def _do_post_password_steps(
    page: Page,
    totp_secret: str,
    email_provider: EmailProvider | None,
    timeout_s: float = _POST_PASSWORD_CODE_WAIT_S,
) -> None:
    """Обрабатывает шаги после ввода пароля: email-code и/или 2FA TOTP.

    OpenAI может запросить код подтверждения в двух сценариях:
    1. **Email-code** — логин с нового IP/device. Код приходит на почту, читаем через email_provider.
    2. **2FA TOTP** — если включена. Генерируем из totp_secret.

    Оба поля — `input[name="code"]`. Различаем по контексту: email-code появляется первым
    (сразу после пароля), 2FA — после. Стратегия: ждём появления поля.
      - Если задан email_provider → запрашиваем email-code, fill, Continue.
      - Затем снова ждём поле; если задан totp_secret → генерируем TOTP, fill, Continue.

    Если код не появился — аккаунт без подтверждений, ничего не делаем.
    """
    # Шаг 1: потенциальный email-code (появляется первым после пароля).
    if email_provider is not None:
        code_input = page.locator('input[name="code"], input[inputmode="numeric"]').first
        try:
            await code_input.wait_for(timeout=int(timeout_s * 1000))
        except PlaywrightTimeoutError:
            # Поля нет → email-code не требуется. Переходим к проверке 2FA.
            code_input = None

        if code_input is not None:
            email_code = await email_provider.fetch_verification_code()
            if not email_code:
                raise OAuthLoginError(
                    "требуется email-code подтверждение, но email_provider вернул None"
                )
            await code_input.fill(email_code)
            await page.get_by_role("button", name="Continue").click()

    # Шаг 2: потенциальный 2FA TOTP (появляется после email-code или сразу после пароля,
    # если email_provider не задан / email-code не требовался).
    if totp_secret:
        otp_input = page.locator('input[name="code"], input[inputmode="numeric"]').first
        try:
            await otp_input.wait_for(timeout=int(timeout_s * 1000))
            code = generate_totp(totp_secret)
            await otp_input.fill(code)
            await page.get_by_role("button", name="Continue").click()
        except PlaywrightTimeoutError:
            # 2FA не потребовалось — нормальный сценарий для некоторых аккаунтов.
            pass


async def login_and_get_auth_code(
    context: BrowserContext,
    login: str,
    password: str,
    totp_secret: str = "",
    timeout_ms: int = 60_000,
    email_provider: EmailProvider | None = None,
) -> tuple[str, str]:
    """Логинится на auth.openai.com и возвращает (authorization_code, code_verifier).

    code_verifier нужен вызывающему для обмена кода на токены (exchange_code_for_tokens),
    поэтому возвращаем оба значения из одного вызова — не нужен модуль-level state.

    Поддержка подтверждений после пароля:
      - email_provider задан → обрабатывает email-code (логин с нового IP).
      - totp_secret задан → обрабатывает 2FA TOTP.
    Оба параметра опциональны; если оба пусты — логин без подтверждений.

    Обратная совместимость: вызов `login_and_get_auth_code(context, login, password, totp_secret)`
    (позиционно) продолжает работать — totp_secret теперь имеет дефолт "".

    Селекторы OpenAI могут меняться — при сбое на реальном аккаунте уточнять тут.
    """
    verifier, challenge = _generate_pkce()
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    auth_url = _build_authorize_url(challenge, state)

    page = await context.new_page()
    auth_code_holder: dict[str, str] = {}

    # Ловим redirect на localhost — там будет authorization_code
    async def _capture_code(response) -> None:
        if "/auth/callback" in response.url and "code=" in response.url:
            params = parse_qs(urlparse(response.url).query)
            code = params.get("code", [None])[0]
            if code:
                auth_code_holder["code"] = code

    page.on("response", _capture_code)

    try:
        await page.goto(auth_url, wait_until="networkidle", timeout=timeout_ms)

        # Шаг 1: ввод email
        email_input = page.locator('input[name="email"], input[type="email"]').first
        await email_input.fill(login)
        await page.get_by_role("button", name="Continue").click()

        # Шаг 2: ввод пароля
        password_input = page.locator('input[name="password"], input[type="password"]').first
        await password_input.fill(password)
        await page.get_by_role("button", name="Continue").click()

        # Шаг 3: email-code и/или 2FA (TOTP), если OpenAI требует.
        await _do_post_password_steps(page, totp_secret, email_provider)

        # Ждём, пока redirect на callback принесёт код
        await asyncio.wait_for(_wait_for_code(auth_code_holder), timeout=timeout_ms / 1000)

    except PlaywrightTimeoutError as e:
        raise OAuthLoginError(f"таймаут при логине: {e}") from e
    except asyncio.TimeoutError as e:
        raise OAuthLoginError("не получен authorization_code за отведённое время") from e
    finally:
        await page.close()

    return auth_code_holder["code"], verifier


async def _wait_for_code(holder: dict[str, str]) -> None:
    """Поллинг-ожидание, пока обработчик response не положит код в holder."""
    while "code" not in holder:
        await asyncio.sleep(0.5)
