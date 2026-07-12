import enum
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openai.client import OpenAIClient
from app.integrations.openai.exceptions import BackendApiError, RefreshFailedError, TokenExpiredError
from app.integrations.openai.oauth import refresh_access_token
from app.models.account import AccountLimits
from app.services.crypto import decrypt, encrypt

# access_token считается свежим, если истекает не раньше чем через это время
_TOKEN_FRESH_THRESHOLD = timedelta(minutes=5)
# Скв: при 401 от backend-api делаем refresh и ретраим замер один раз
_MAX_RETRIES = 1


class MeasureResult(enum.Enum):
    OK = "ok"
    REFRESH_FAILED = "refresh_failed"
    BACKEND_ERROR = "backend_error"


async def measure_account_limits(session: AsyncSession, account_id: int) -> MeasureResult:
    """Замеряет лимиты и подписку аккаунта, обновляет AccountLimits.

    Цикл: refresh access_token (если протух) → get_usage + get_account_metadata → запись в БД.
    При RefreshFailedError → refresh_status=expired, возврат REFRESH_FAILED.
    """
    limits = await session.get(AccountLimits, account_id)
    if limits is None:
        raise ValueError(f"AccountLimits not found for account_id={account_id}")

    access_token = decrypt(limits.access_token_encrypted) if limits.access_token_encrypted else None
    if access_token is None or _is_token_expired(limits.access_token_expires_at):
        refreshed = await _do_refresh(session, limits)
        if refreshed is None:
            return MeasureResult.REFRESH_FAILED
        access_token = refreshed

    # Замер с retry при 401
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with OpenAIClient(access_token, limits.account_id_openai) as client:
                usage = await client.get_usage()
                metadata = await client.get_account_metadata()
            break
        except TokenExpiredError:
            if attempt >= _MAX_RETRIES:
                raise
            refreshed = await _do_refresh(session, limits)
            if refreshed is None:
                return MeasureResult.REFRESH_FAILED
            access_token = refreshed
        except BackendApiError:
            return MeasureResult.BACKEND_ERROR

    # Запись результатов: chat/codex равны (общий rate_limit)
    primary = usage.primary_remaining_pct
    secondary = usage.secondary_remaining_pct
    limits.chat_5h_remaining_pct = primary
    limits.codex_5h_remaining_pct = primary
    limits.chat_weekly_remaining_pct = secondary
    limits.codex_weekly_remaining_pct = secondary
    limits.plan_type = metadata.plan_type or usage.plan_type
    limits.subscription_expires_at = metadata.subscription_expires_at
    limits.measured_at = datetime.now(timezone.utc)
    limits.refresh_status = "ok"
    limits.refresh_failed_at = None

    await session.commit()
    return MeasureResult.OK


def _is_token_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return True
    return expires_at <= datetime.now(timezone.utc) + _TOKEN_FRESH_THRESHOLD


async def _do_refresh(session: AsyncSession, limits: AccountLimits) -> str | None:
    """Обновляет access_token. При провале — ставит refresh_status=expired, возвращает None."""
    try:
        refreshed = await refresh_access_token(decrypt(limits.refresh_token_encrypted))
    except RefreshFailedError:
        limits.refresh_status = "expired"
        limits.refresh_failed_at = datetime.now(timezone.utc)
        limits.refresh_recover_attempts += 1
        await session.commit()
        return None

    limits.access_token_encrypted = encrypt(refreshed.access_token)
    limits.refresh_token_encrypted = encrypt(refreshed.refresh_token)
    limits.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    limits.refresh_recover_attempts = 0
    limits.refresh_status = "ok"
    await session.commit()
    return refreshed.access_token
