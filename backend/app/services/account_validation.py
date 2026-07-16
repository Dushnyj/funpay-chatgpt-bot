from __future__ import annotations

import enum
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.email.imap_provider import detect_imap_provider
from app.integrations.email.microsoft_graph_provider import (
    MicrosoftGraphEmailProvider,
)
from app.integrations.email.provider import EmailProvider, EmailProviderError
from app.integrations.openai.oauth import (
    IdTokenClaims,
    exchange_code_for_tokens,
    parse_id_token,
)
from app.integrations.playwright.browser import browser_context
from app.integrations.playwright.proxy import (
    BrowserProxy,
    ProxyUnavailableError,
    is_proxy_failure,
)
from app.integrations.playwright.enable_2fa import Enable2FAError, enable_2fa
from app.integrations.playwright.oauth_login import OAuthLoginError, login_and_get_auth_code
from app.config import get_settings
from app.models.account import Account, AccountLimits, EmailOAuthCredential
from app.models.proxy_route import ProxyRoute
from app.models.settings import SellerSettings
from app.services.account_limits import (
    MeasureResult,
    _UNRESOLVED_PROXY,
    _store_subscription_expiry,
    measure_account_limits,
)
from app.services.totp import is_valid_base32
from app.services.proxy_routes import mark_proxy_route_offline, resolve_browser_proxy

logger = logging.getLogger(__name__)

_REDIRECT_URI = "http://localhost:1455/auth/callback"


class ValidationOutcome(str, enum.Enum):
    """Successful terminal outcome. Failures use ``AccountValidationError``."""

    OK = "ok"
    # Compatibility for injected validators used by the replacement handler.
    # validate_account itself never returns these failure values anymore.
    LOGIN_FAILED = "login_failed"
    MEASURE_FAILED = "measure_failed"
    INVALID_2FA = "invalid_2fa"
    SETUP_2FA_FAILED = "setup_2fa_failed"


class ValidationStage(str, enum.Enum):
    INPUT = "input"
    EMAIL_PREFLIGHT = "email_preflight"
    LOGIN = "login"
    SETUP_2FA = "setup_2fa"
    TOKEN_EXCHANGE = "token_exchange"
    LIMIT_MEASUREMENT = "limit_measurement"
    INTERNAL = "internal"
    PROXY = "proxy"


class ValidationCode(str, enum.Enum):
    INVALID_TOTP = "invalid_totp"
    MISSING_2FA_DATA = "missing_2fa_data"
    INVALID_CREDENTIALS = "invalid_credentials"
    LOGIN_TIMEOUT = "login_timeout"
    OAUTH_REJECTED = "oauth_rejected"
    OAUTH_CALLBACK_INVALID = "oauth_callback_invalid"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
    EMAIL_AUTH_FAILED = "email_auth_failed"
    EMAIL_CODE_NOT_FOUND = "email_code_not_found"
    EMAIL_CODE_REJECTED = "email_code_rejected"
    EMAIL_PROVIDER_UNSUPPORTED = "email_provider_unsupported"
    EMAIL_CONNECTION_FAILED = "email_connection_failed"
    EMAIL_SECURITY_CHALLENGE = "email_security_challenge"
    EMAIL_TIMEOUT = "email_timeout"
    SETUP_2FA_FAILED = "setup_2fa_failed"
    SETUP_2FA_UI_TIMEOUT = "setup_2fa_ui_timeout"
    SETUP_2FA_BUTTON_NOT_FOUND = "setup_2fa_button_not_found"
    SETUP_2FA_QR_NOT_FOUND = "setup_2fa_qr_not_found"
    SETUP_2FA_QR_INVALID = "setup_2fa_qr_invalid"
    TOKEN_EXCHANGE_FAILED = "token_exchange_failed"
    MEASURE_FAILED = "measure_failed"
    PLAN_DETECTION_FAILED = "plan_detection_failed"
    PLAN_WINDOW_MISMATCH = "plan_window_mismatch"
    INTERNAL_ERROR = "internal_error"
    PROXY_UNAVAILABLE = "proxy_unavailable"
    PROXY_ROUTE_CHANGED = "proxy_route_changed"


class AccountValidationError(RuntimeError):
    """A secret-free validation failure persisted verbatim in the job record."""

    def __init__(
        self,
        stage: ValidationStage | str,
        code: ValidationCode | str,
        detail: str,
    ) -> None:
        self.stage = stage.value if isinstance(stage, ValidationStage) else stage
        self.code = code.value if isinstance(code, ValidationCode) else code
        self.detail = detail
        super().__init__(detail)

    def as_dict(self) -> dict[str, str]:
        return {"stage": self.stage, "code": self.code, "detail": self.detail}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))


async def validate_account(session: AsyncSession, account_id: int) -> ValidationOutcome:
    """Run the complete login, token and limits validation pipeline.

    ``active`` is written only after all stages succeed. Every diagnosed failure
    marks the account ``validation_failed`` and raises ``AccountValidationError``
    so the worker can persist a structured, user-facing reason on the job.
    """
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account not found: {account_id}")

    browser_proxy: BrowserProxy | None = None
    try:
        login = account.login
        password = account.password_encrypted
        totp_secret = account.totp_secret_encrypted
        email = account.email
        email_password = account.email_password_encrypted or None

        if totp_secret and not is_valid_base32(totp_secret):
            raise AccountValidationError(
                ValidationStage.INPUT,
                ValidationCode.INVALID_TOTP,
                "Сохранённый TOTP-секрет имеет неверный формат.",
            )

        browser_proxy = await resolve_browser_proxy(session, account)
        email_provider = await _build_email_provider(
            session,
            account,
            email,
            email_password,
            browser_proxy=browser_proxy,
        )

        if totp_secret:
            return await _validate_with_existing_totp(
                session,
                account,
                login,
                password,
                totp_secret,
                email_provider,
                browser_proxy,
            )
        if email_provider is not None:
            await _preflight_email_provider(email_provider)
            return await _validate_and_enable_2fa(
                session,
                account,
                login,
                password,
                email_provider,
                browser_proxy,
            )

        raise AccountValidationError(
            ValidationStage.INPUT,
            ValidationCode.MISSING_2FA_DATA,
            "Нужен корректный TOTP-секрет либо почта с паролем приложения.",
        )
    except ProxyUnavailableError as exc:
        account.status = "validation_failed"
        await mark_proxy_route_offline(session, browser_proxy)
        await session.flush()
        raise AccountValidationError(
            ValidationStage.PROXY,
            ValidationCode.PROXY_UNAVAILABLE,
            exc.detail,
        ) from exc
    except AccountValidationError:
        account.status = "validation_failed"
        await session.flush()
        raise
    except Exception as exc:
        account.status = "validation_failed"
        await session.flush()
        if browser_proxy is not None and is_proxy_failure(exc):
            await mark_proxy_route_offline(session, browser_proxy)
            raise AccountValidationError(
                ValidationStage.PROXY,
                ValidationCode.PROXY_UNAVAILABLE,
                "Маршрут входа через прокси недоступен.",
            ) from exc
        logger.exception("Unexpected account validation failure for account %s", account_id)
        raise AccountValidationError(
            ValidationStage.INTERNAL,
            ValidationCode.INTERNAL_ERROR,
            "Внутренняя ошибка проверки аккаунта.",
        ) from exc


async def _build_email_provider(
    session: AsyncSession,
    account: Account,
    email: str | None,
    email_password: str | None,
    *,
    browser_proxy: BrowserProxy | None = None,
) -> EmailProvider | None:
    if not email:
        return None

    credential = await session.get(EmailOAuthCredential, account.id)
    if (
        credential is not None
        and credential.provider == "microsoft_graph"
        and credential.status == "connected"
        and credential.email.strip().casefold() == email.strip().casefold()
    ):
        settings = get_settings()
        client_id = settings.microsoft_graph_client_id.strip()
        client_secret = settings.microsoft_graph_client_secret.strip()
        if client_id and client_secret:

            async def persist_refresh_token(refresh_token: str) -> None:
                credential.refresh_token_encrypted = refresh_token
                credential.updated_at = datetime.now(timezone.utc)
                try:
                    # The caller owns the transaction and may be holding row
                    # locks that enforce rental/replacement invariants.  A
                    # provider callback must never release those locks.
                    await session.flush()
                except Exception:
                    await session.rollback()
                    raise

            async def mark_reauthorization_required() -> None:
                credential.status = "reauthorization_required"
                credential.updated_at = datetime.now(timezone.utc)
                try:
                    await session.flush()
                except Exception:
                    await session.rollback()
                    raise

            return MicrosoftGraphEmailProvider(
                email,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=credential.refresh_token_encrypted,
                on_refresh_token=persist_refresh_token,
                on_reauthorization_required=mark_reauthorization_required,
                browser_proxy=browser_proxy,
            )
        if not email_password:
            raise AccountValidationError(
                ValidationStage.EMAIL_PREFLIGHT,
                ValidationCode.EMAIL_PROVIDER_UNSUPPORTED,
                "Microsoft Graph OAuth не настроен на сервере.",
            )

    if not email_password:
        return None
    if browser_proxy is None:
        return detect_imap_provider(email, email_password)
    return detect_imap_provider(
        email, email_password, browser_proxy=browser_proxy
    )


async def _preflight_email_provider(provider: EmailProvider) -> None:
    try:
        await provider.preflight()
    except EmailProviderError as exc:
        if is_proxy_failure(exc):
            raise ProxyUnavailableError(
                "Маршрут входа в почту через прокси недоступен.",
            ) from exc
        raise AccountValidationError(
            ValidationStage.EMAIL_PREFLIGHT,
            exc.code.value,
            exc.detail,
        ) from exc
    except Exception as exc:
        if is_proxy_failure(exc):
            raise ProxyUnavailableError(
                "Маршрут входа в почту через прокси недоступен.",
            ) from exc
        raise AccountValidationError(
            ValidationStage.EMAIL_PREFLIGHT,
            ValidationCode.EMAIL_CONNECTION_FAILED,
            "Не удалось подключиться к почтовому серверу.",
        ) from exc


def _from_oauth_error(exc: OAuthLoginError) -> AccountValidationError:
    try:
        code: ValidationCode | str = ValidationCode(exc.code.value)
    except ValueError:
        code = exc.code.value
    return AccountValidationError(exc.stage, code, exc.detail)


async def _validate_with_existing_totp(
    session: AsyncSession,
    account: Account,
    login: str,
    password: str,
    totp_secret: str,
    email_provider: EmailProvider | None,
    browser_proxy: BrowserProxy | None,
) -> ValidationOutcome:
    try:
        async with browser_context(proxy=browser_proxy) as context:
            auth_code, code_verifier = await login_and_get_auth_code(
                context,
                login,
                password,
                totp_secret,
                email_provider=email_provider,
            )
    except OAuthLoginError as exc:
        raise _from_oauth_error(exc) from exc

    tokens = await _exchange_tokens(auth_code, code_verifier, browser_proxy)
    return await _save_tokens_and_measure(
        session, account, tokens, browser_proxy=browser_proxy
    )


async def _validate_and_enable_2fa(
    session: AsyncSession,
    account: Account,
    login: str,
    password: str,
    email_provider: EmailProvider,
    browser_proxy: BrowserProxy | None,
) -> ValidationOutcome:
    try:
        async with browser_context(proxy=browser_proxy) as context:
            secret = await enable_2fa(context, login, password, email_provider)
            account.totp_secret_encrypted = secret
            # Enabling 2FA is a remote irreversible side effect. Persist the
            # returned secret before any subsequent browser/token work so a
            # process cancellation cannot lock the account with an unknown key.
            await session.commit()

            auth_code, code_verifier = await login_and_get_auth_code(
                context,
                login,
                password,
                secret,
                email_provider=email_provider,
            )
    except OAuthLoginError as exc:
        raise _from_oauth_error(exc) from exc
    except Enable2FAError as exc:
        raise AccountValidationError(exc.stage, exc.code, exc.detail) from exc

    tokens = await _exchange_tokens(auth_code, code_verifier, browser_proxy)
    return await _save_tokens_and_measure(
        session, account, tokens, browser_proxy=browser_proxy
    )


async def _exchange_tokens(
    auth_code: str,
    code_verifier: str,
    browser_proxy: BrowserProxy | None,
):
    try:
        if browser_proxy is None:
            return await exchange_code_for_tokens(
                auth_code, code_verifier, _REDIRECT_URI
            )
        return await exchange_code_for_tokens(
            auth_code,
            code_verifier,
            _REDIRECT_URI,
            proxy=browser_proxy,
        )
    except ProxyUnavailableError:
        raise
    except Exception as exc:
        raise AccountValidationError(
            ValidationStage.TOKEN_EXCHANGE,
            ValidationCode.TOKEN_EXCHANGE_FAILED,
            "OpenAI не выдал токены после успешного входа.",
        ) from exc


async def _save_tokens_and_measure(
    session: AsyncSession,
    account: Account,
    tokens,
    *,
    browser_proxy: BrowserProxy | None | object = _UNRESOLVED_PROXY,
) -> ValidationOutcome:
    if browser_proxy is not _UNRESOLVED_PROXY:
        await assert_proxy_selection_unchanged(
            session,
            account,
            browser_proxy,
            lock_account=True,
        )
    claims = parse_id_token(tokens.id_token) if tokens.id_token else IdTokenClaims()
    if not _identity_matches(account, claims.email):
        raise AccountValidationError(
            ValidationStage.LOGIN,
            ValidationCode.INVALID_CREDENTIALS,
            "OpenAI подтвердил другой аккаунт.",
        )

    limits = await session.get(AccountLimits, account.id)
    if limits is None:
        limits = AccountLimits(account_id=account.id, refresh_token_encrypted=tokens.refresh_token)
        session.add(limits)

    limits.refresh_token_encrypted = tokens.refresh_token
    limits.access_token_encrypted = tokens.access_token
    limits.account_id_openai = claims.account_id
    limits.refresh_status = "ok"
    limits.refresh_recover_attempts = 0
    limits.refresh_last_recover_at = None

    if claims.subscription_expires_at:
        _store_subscription_expiry(
            account,
            limits,
            claims.subscription_expires_at,
            source="id_token",
        )

    await session.flush()

    try:
        result = await measure_account_limits(
            session,
            account.id,
            claim_plan_type=claims.plan_type,
            browser_proxy=browser_proxy,
        )
    except ProxyUnavailableError:
        raise
    except Exception as exc:
        raise AccountValidationError(
            ValidationStage.LIMIT_MEASUREMENT,
            ValidationCode.MEASURE_FAILED,
            "Вход выполнен, но лимиты аккаунта получить не удалось.",
        ) from exc
    if result is MeasureResult.PLAN_DETECTION_FAILED:
        raise AccountValidationError(
            ValidationStage.LIMIT_MEASUREMENT,
            ValidationCode.PLAN_DETECTION_FAILED,
            "OpenAI не вернул однозначный поддерживаемый тариф аккаунта.",
        )
    if result is MeasureResult.PLAN_WINDOW_MISMATCH:
        raise AccountValidationError(
            ValidationStage.LIMIT_MEASUREMENT,
            ValidationCode.PLAN_WINDOW_MISMATCH,
            "Тариф определён, но длительность лимита не соответствует плану.",
        )
    if result is not MeasureResult.OK:
        raise AccountValidationError(
            ValidationStage.LIMIT_MEASUREMENT,
            ValidationCode.MEASURE_FAILED,
            "Вход выполнен, но лимиты аккаунта получить не удалось.",
        )

    if browser_proxy is not _UNRESOLVED_PROXY:
        # Limit measurement commits while doing remote I/O, so reacquire the
        # Account row here and keep it locked through the caller's final job
        # commit. This closes the last route-mutation window before ``active``.
        await assert_proxy_selection_unchanged(
            session,
            account,
            browser_proxy,
            lock_account=True,
        )

    # Re-read durable operator intent after the network/browser work. An admin
    # may have paused the account in another transaction while validation was
    # running; that decision must win over a late successful response.
    await session.refresh(account, attribute_names=["operator_status_override"])
    account.status = account.operator_status_override or "active"
    account.chatgpt_last_check_at = datetime.now(timezone.utc)
    await session.flush()
    return ValidationOutcome.OK


async def assert_proxy_selection_unchanged(
    session: AsyncSession,
    account: Account,
    expected: BrowserProxy | None,
    *,
    lock_account: bool = False,
) -> None:
    """Fail closed when the effective Direct/proxy selection has changed.

    The snapshot includes the route id, revision, endpoint and credentials.
    ``lock_account`` is used immediately before token persistence; every admin
    route mutation locks the same Account row first, so it cannot slip between
    this comparison and the worker's commit.
    """

    statement = select(Account.id, Account.proxy_route_id).where(
        Account.id == account.id
    )
    if lock_account:
        statement = statement.with_for_update()
    account_row = (await session.execute(statement)).one_or_none()
    if account_row is None:
        raise ValueError(f"Account not found: {account.id}")

    route_id = account_row.proxy_route_id
    if route_id is None:
        route_id = await session.scalar(
            select(SellerSettings.default_proxy_route_id).where(
                SellerSettings.id == 1
            )
        )

    current: BrowserProxy | None
    if route_id is None:
        current = None
    else:
        route_row = (
            await session.execute(
                select(
                    ProxyRoute.id,
                    ProxyRoute.proxy_type,
                    ProxyRoute.host,
                    ProxyRoute.port,
                    ProxyRoute.username_encrypted,
                    ProxyRoute.password_encrypted,
                    ProxyRoute.config_revision,
                ).where(ProxyRoute.id == route_id)
            )
        ).one_or_none()
        if route_row is None:
            current = None
        else:
            current = BrowserProxy(
                route_id=route_row.id,
                proxy_type=route_row.proxy_type,
                host=route_row.host,
                port=route_row.port,
                username=route_row.username_encrypted or None,
                password=route_row.password_encrypted or None,
                config_revision=route_row.config_revision,
            )

    if _same_proxy_selection(expected, current):
        return

    # The worker consumes this durable bit after recording the typed failure
    # and queues one validation against the new route. Device Auth consumes it
    # through its own failure path in the same way.
    account.validation_rerun_requested = True
    raise AccountValidationError(
        ValidationStage.PROXY,
        ValidationCode.PROXY_ROUTE_CHANGED,
        "Маршрут входа изменился во время проверки; проверка запущена заново.",
    )


def _same_proxy_selection(
    first: BrowserProxy | None,
    second: BrowserProxy | None,
) -> bool:
    if first is None or second is None:
        return first is second
    return (
        first.route_id == second.route_id
        and first.config_revision == second.config_revision
        and first.proxy_type == second.proxy_type
        and first.host == second.host
        and first.port == second.port
        and first.username == second.username
        and first.password == second.password
    )


def _identity_matches(account: Account, token_email: str | None) -> bool:
    if not token_email:
        return False
    expected = {
        value.strip().casefold()
        for value in (account.login, account.email)
        if value and "@" in value
    }
    return token_email.strip().casefold() in expected
