import asyncio
import base64
import hashlib
import secrets
from urllib.parse import urlencode, parse_qs, urlparse

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeoutError

from app.integrations.openai.oauth import OPENAI_CLIENT_ID, OPENAI_ISSUER
from app.services.totp import generate_totp

# URL OAuth-авторизации Codex CLI (реверс-инжиниринг codex-switcher)
_AUTHORIZE_BASE = f"{OPENAI_ISSUER}/oauth/authorize"
_REDIRECT_URI = "http://localhost:1455/auth/callback"
_SCOPE = "openid profile email offline_access"


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


async def login_and_get_auth_code(
    context: BrowserContext,
    login: str,
    password: str,
    totp_secret: str,
    timeout_ms: int = 60_000,
) -> tuple[str, str]:
    """Логинится на auth.openai.com и возвращает (authorization_code, code_verifier).

    code_verifier нужен вызывающему для обмена кода на токены (exchange_code_for_tokens),
    поэтому возвращаем оба значения из одного вызова — не нужен модуль-level state.

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

        # Шаг 3: 2FA (TOTP), если OpenAI требует
        try:
            otp_input = page.locator('input[name="code"], input[inputmode="numeric"]').first
            await otp_input.wait_for(timeout=15_000)
            code = generate_totp(totp_secret)
            await otp_input.fill(code)
            await page.get_by_role("button", name="Continue").click()
        except PlaywrightTimeoutError:
            # 2FA не потребовалось — нормальный сценарий для некоторых аккаунтов
            pass

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
