import enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openai.oauth import (
    IdTokenClaims,
    exchange_code_for_tokens,
    parse_id_token,
)
from app.integrations.playwright.browser import browser_context
from app.integrations.playwright.oauth_login import OAuthLoginError, login_and_get_auth_code
from app.models.account import Account, AccountLimits
from app.services.account_limits import MeasureResult, measure_account_limits
from app.services.crypto import decrypt, encrypt

_REDIRECT_URI = "http://localhost:1455/auth/callback"


class ValidationOutcome(enum.Enum):
    OK = "ok"
    LOGIN_FAILED = "login_failed"
    MEASURE_FAILED = "measure_failed"


async def validate_account(session: AsyncSession, account_id: int) -> ValidationOutcome:
    """Первичная валидация аккаунта через Playwright OAuth flow.

    Шаги: логин → auth code → exchange → сохранение токенов → первичный замер лимитов.
    При успехе аккаунт → active. При сбое логина — остаётся в текущем статусе.
    """
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account not found: {account_id}")

    login = account.login
    password = decrypt(account.password_encrypted)
    totp_secret = decrypt(account.totp_secret_encrypted)

    # Playwright OAuth flow: получаем auth code + PKCE verifier одним вызовом
    try:
        async with browser_context() as context:
            auth_code, code_verifier = await login_and_get_auth_code(
                context, login, password, totp_secret
            )
            tokens = await exchange_code_for_tokens(auth_code, code_verifier, _REDIRECT_URI)
    except OAuthLoginError:
        return ValidationOutcome.LOGIN_FAILED

    # Парсинг id_token → claims (email, plan_type, account_id, ...)
    claims = parse_id_token(tokens.id_token) if tokens.id_token else IdTokenClaims()

    # Создание/обновление AccountLimits с полученными токенами
    limits = await session.get(AccountLimits, account_id)
    if limits is None:
        limits = AccountLimits(account_id=account_id, refresh_token_encrypted=encrypt(tokens.refresh_token))
        session.add(limits)

    limits.refresh_token_encrypted = encrypt(tokens.refresh_token)
    limits.access_token_encrypted = encrypt(tokens.access_token)
    limits.account_id_openai = claims.account_id
    limits.refresh_status = "ok"
    limits.refresh_recover_attempts = 0

    # Подписка из claims — полезно для прогноза истечения аренды
    if claims.subscription_expires_at:
        account.subscription_expires_at = claims.subscription_expires_at

    await session.commit()

    # Первичный замер лимитов: аккаунт валиден, но если замер упал — это не блокер
    result = await measure_account_limits(session, account_id)
    if result != MeasureResult.OK:
        return ValidationOutcome.MEASURE_FAILED

    account.status = "active"
    await session.commit()
    return ValidationOutcome.OK
