import enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.email.imap_provider import detect_imap_provider
from app.integrations.openai.oauth import (
    IdTokenClaims,
    exchange_code_for_tokens,
    parse_id_token,
)
from app.integrations.playwright.browser import browser_context
from app.integrations.playwright.enable_2fa import Enable2FAError, enable_2fa
from app.integrations.playwright.oauth_login import OAuthLoginError, login_and_get_auth_code
from app.models.account import Account, AccountLimits
from app.services.account_limits import MeasureResult, measure_account_limits
from app.services.totp import is_valid_base32

_REDIRECT_URI = "http://localhost:1455/auth/callback"


class ValidationOutcome(enum.Enum):
    OK = "ok"
    LOGIN_FAILED = "login_failed"
    MEASURE_FAILED = "measure_failed"
    INVALID_2FA = "invalid_2fa"  # totp_secret кривой или нет данных для включения
    SETUP_2FA_FAILED = "setup_2fa_failed"  # попытка включить 2FA провалилась


async def validate_account(session: AsyncSession, account_id: int) -> ValidationOutcome:
    """Первичная валидация аккаунта через Playwright OAuth flow.

    Ветвление по 2FA:
      - totp_secret валиден → обычный flow с существующим TOTP.
      - totp_secret кривой → INVALID_2FA.
      - totp_secret пуст + email задан → пробуем включить 2FA автоматически.
      - иначе → INVALID_2FA (нечем работать).
    """
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account not found: {account_id}")

    login = account.login
    password = account.password_encrypted
    totp_secret = account.totp_secret_encrypted
    email = account.email
    email_password = (
        account.email_password_encrypted if account.email_password_encrypted else None
    )

    # Ветвление по 2FA
    if totp_secret and is_valid_base32(totp_secret):
        # Обычный flow с существующим TOTP
        return await _validate_with_existing_totp(session, account, login, password, totp_secret)
    elif totp_secret and not is_valid_base32(totp_secret):
        # Секрет передан но кривой
        return ValidationOutcome.INVALID_2FA
    elif email and email_password:
        # Нет TOTP, но есть email → пробуем включить 2FA
        return await _validate_and_enable_2fa(
            session, account, login, password, email, email_password
        )
    else:
        # Нет TOTP и нет email — нечем работать
        return ValidationOutcome.INVALID_2FA


async def _validate_with_existing_totp(
    session: AsyncSession,
    account: Account,
    login: str,
    password: str,
    totp_secret: str,
) -> ValidationOutcome:
    """Текущий flow: логин с существующим TOTP → токены → замер лимитов."""
    # Playwright OAuth flow: получаем auth code + PKCE verifier одним вызовом
    try:
        async with browser_context() as context:
            auth_code, code_verifier = await login_and_get_auth_code(
                context, login, password, totp_secret
            )
            tokens = await exchange_code_for_tokens(auth_code, code_verifier, _REDIRECT_URI)
    except OAuthLoginError:
        return ValidationOutcome.LOGIN_FAILED

    return await _save_tokens_and_measure(session, account, tokens)


async def _validate_and_enable_2fa(
    session: AsyncSession,
    account: Account,
    login: str,
    password: str,
    email: str,
    email_password: str,
) -> ValidationOutcome:
    """Flow: логин → включение 2FA → сохранение secret → обычная валидация."""
    email_provider = detect_imap_provider(email, email_password)
    try:
        async with browser_context() as context:
            # Шаг 1: включаем 2FA, получаем secret
            secret = await enable_2fa(context, login, password, email_provider)
            # FernetEncrypted encrypts on write; ORM attributes stay plaintext.
            account.totp_secret_encrypted = secret
            await session.commit()

            # Шаг 2: логин с новым TOTP для получения токенов
            auth_code, code_verifier = await login_and_get_auth_code(
                context, login, password, secret
            )
            tokens = await exchange_code_for_tokens(auth_code, code_verifier, _REDIRECT_URI)
    except OAuthLoginError:
        return ValidationOutcome.LOGIN_FAILED
    except Enable2FAError:
        return ValidationOutcome.SETUP_2FA_FAILED
    except Exception:
        return ValidationOutcome.SETUP_2FA_FAILED

    return await _save_tokens_and_measure(session, account, tokens)


async def _save_tokens_and_measure(
    session: AsyncSession,
    account: Account,
    tokens,
) -> ValidationOutcome:
    """Общий хвост валидации: парсинг claims → сохранение токенов → замер → active."""
    # Парсинг id_token → claims (email, plan_type, account_id, ...)
    claims = parse_id_token(tokens.id_token) if tokens.id_token else IdTokenClaims()

    # Создание/обновление AccountLimits с полученными токенами
    limits = await session.get(AccountLimits, account.id)
    if limits is None:
        limits = AccountLimits(account_id=account.id, refresh_token_encrypted=tokens.refresh_token)
        session.add(limits)

    limits.refresh_token_encrypted = tokens.refresh_token
    limits.access_token_encrypted = tokens.access_token
    limits.account_id_openai = claims.account_id
    limits.refresh_status = "ok"
    limits.refresh_recover_attempts = 0

    # Подписка из claims — полезно для прогноза истечения аренды
    if claims.subscription_expires_at:
        account.subscription_expires_at = claims.subscription_expires_at

    await session.commit()

    # Первичный замер лимитов: аккаунт валиден, но если замер упал — это не блокер
    result = await measure_account_limits(session, account.id)
    if result != MeasureResult.OK:
        return ValidationOutcome.MEASURE_FAILED

    account.status = "active"
    await session.commit()
    return ValidationOutcome.OK
