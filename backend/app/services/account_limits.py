import enum
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openai.client import OpenAIClient
from app.integrations.openai.exceptions import BackendApiError, RefreshFailedError, TokenExpiredError
from app.integrations.openai.oauth import parse_id_token, refresh_access_token
from app.integrations.openai.types import AccountMetadata
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier
from app.services.subscription_plans import (
    PLANS_BY_CODE,
    PlanSignal,
    ResolvedSubscriptionPlan,
    resolve_subscription_plan,
)

# access_token считается свежим, если истекает не раньше чем через это время
_TOKEN_FRESH_THRESHOLD = timedelta(minutes=5)
# Скв: при 401 от backend-api делаем refresh и ретраим замер один раз
_MAX_RETRIES = 1
_FIVE_HOURS_SECONDS = 5 * 60 * 60
_ONE_WEEK_SECONDS = 7 * 24 * 60 * 60


class MeasureResult(enum.Enum):
    OK = "ok"
    REFRESH_FAILED = "refresh_failed"
    BACKEND_ERROR = "backend_error"


async def measure_account_limits(
    session: AsyncSession,
    account_id: int,
    *,
    claim_plan_type: str | None = None,
) -> MeasureResult:
    """Замеряет лимиты и подписку аккаунта, обновляет AccountLimits.

    Цикл: refresh access_token (если протух) → get_usage + get_account_metadata → запись в БД.
    При RefreshFailedError → refresh_status=expired, возврат REFRESH_FAILED.
    """
    limits = await session.get(AccountLimits, account_id)
    if limits is None:
        raise ValueError(f"AccountLimits not found for account_id={account_id}")
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account not found for account_id={account_id}")

    access_token = limits.access_token_encrypted
    if access_token is None or _is_token_expired(limits.access_token_expires_at):
        refreshed = await _do_refresh(session, limits)
        if refreshed is None:
            return MeasureResult.REFRESH_FAILED
        access_token = refreshed

    # Current OpenAI access tokens carry the ChatGPT account ID and plan in the
    # auth namespace. Older id_token-derived values remain a supported fallback.
    access_claims = parse_id_token(access_token)
    if access_claims.account_id:
        limits.account_id_openai = access_claims.account_id

    # Замер с retry при 401
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with OpenAIClient(access_token, limits.account_id_openai) as client:
                usage = await client.get_usage()
                try:
                    metadata = await client.get_account_metadata()
                except BackendApiError:
                    # ``accounts/check`` is protected independently from the
                    # Codex usage endpoint and may return a Cloudflare 403 from
                    # a datacenter IP. A verified usage payload plus matching
                    # token claims is still enough to identify the current
                    # plan and exact limits; only subscription expiry remains
                    # unknown until metadata is reachable again.
                    metadata = AccountMetadata()
            break
        except TokenExpiredError:
            if attempt >= _MAX_RETRIES:
                raise
            refreshed = await _do_refresh(session, limits)
            if refreshed is None:
                return MeasureResult.REFRESH_FAILED
            access_token = refreshed
            access_claims = parse_id_token(access_token)
            if access_claims.account_id:
                limits.account_id_openai = access_claims.account_id
        except BackendApiError:
            return MeasureResult.BACKEND_ERROR

    # ``wham/usage`` is the observed Codex allowance used by the Codex client.
    # OpenAI does not publish a stable ChatGPT-message allowance API, so never
    # mirror these values into the chat fields and present guessed data as fact.
    limits.chat_5h_remaining_pct = None
    limits.chat_weekly_remaining_pct = None
    limits.codex_primary_remaining_pct = usage.primary_remaining_pct
    limits.codex_primary_window_seconds = usage.primary_window_seconds
    limits.codex_primary_resets_at = usage.primary_resets_at
    limits.codex_secondary_remaining_pct = usage.secondary_remaining_pct
    limits.codex_secondary_window_seconds = usage.secondary_window_seconds
    limits.codex_secondary_resets_at = usage.secondary_resets_at

    # Backward-compatible aliases must not mislabel a 30-day (or any other)
    # observed window as 5h/weekly.
    limits.codex_5h_remaining_pct = (
        usage.primary_remaining_pct
        if usage.primary_window_seconds == _FIVE_HOURS_SECONDS
        else None
    )
    limits.codex_weekly_remaining_pct = (
        usage.secondary_remaining_pct
        if usage.secondary_window_seconds == _ONE_WEEK_SECONDS
        else None
    )
    resolved_plan = resolve_subscription_plan(
        (
            PlanSignal(metadata.plan_type, "accounts_check", 0.98),
            PlanSignal(usage.plan_type, "wham_usage", 0.90),
            PlanSignal(claim_plan_type, "id_token", 0.80),
            PlanSignal(access_claims.plan_type, "access_token", 0.85),
        )
    )
    await _store_resolved_plan(session, account, limits, resolved_plan)
    if metadata.has_active_subscription is True:
        limits.subscription_expires_at = metadata.subscription_expires_at
        account.subscription_expires_at = metadata.subscription_expires_at
    elif metadata.has_active_subscription is False:
        # Inactive entitlements often contain a stale historical expires_at.
        limits.subscription_expires_at = None
        account.subscription_expires_at = None
    elif limits.subscription_expires_at is None and account.subscription_expires_at is not None:
        # /accounts/check legitimately omits expiry for some responses. Keep
        # stronger evidence already stored from an ID token or prior measure.
        limits.subscription_expires_at = account.subscription_expires_at
    limits.measured_at = datetime.now(timezone.utc)
    limits.refresh_status = "ok"
    limits.refresh_failed_at = None

    await session.commit()
    return MeasureResult.OK


async def _store_resolved_plan(
    session: AsyncSession,
    account: Account,
    limits: AccountLimits,
    resolved: ResolvedSubscriptionPlan,
) -> None:
    """Persist both the canonical result and its audit evidence."""

    account.plan_raw_type = resolved.raw
    account.plan_source = resolved.source
    account.plan_confidence = resolved.confidence
    account.plan_detected_at = datetime.now(timezone.utc)

    if resolved.code is None:
        # Never leave the previous sellable tier attached after an unknown or
        # conflicting result.
        account.tier_id = None
        limits.plan_type = "unknown"
        return

    limits.plan_type = resolved.code
    tier = await _get_canonical_tier(session, resolved.code)
    account.tier_id = tier.id


async def _get_canonical_tier(
    session: AsyncSession, code: str
) -> SubscriptionTier:
    definition = PLANS_BY_CODE[code]
    tier = (
        await session.execute(
            select(SubscriptionTier).where(
                or_(
                    SubscriptionTier.code == code,
                    SubscriptionTier.name == definition.name,
                    # Compatibility with the catalog used before Pro 5x/20x
                    # became separate canonical products.
                    *(
                        (SubscriptionTier.name == "Pro",)
                        if code == "pro_20x"
                        else ()
                    ),
                )
            )
        )
    ).scalars().first()
    if tier is None:
        tier = SubscriptionTier(
            code=definition.code,
            name=definition.name,
            description=definition.description,
            is_active=True,
            system_managed=True,
            is_sellable=definition.is_sellable,
            sort_order=definition.sort_order,
            usage_multiplier=definition.usage_multiplier,
        )
        session.add(tier)
        await session.flush()
    elif tier.name == "Pro" and code == "pro_20x":
        tier.name = definition.name

    tier.code = definition.code
    tier.system_managed = True
    tier.sort_order = definition.sort_order
    tier.usage_multiplier = definition.usage_multiplier
    await session.flush()
    return tier


def _is_token_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return True
    # SQLite used in tests drops timezone information; interpret a naive value
    # as UTC, which is also how token expiry is persisted in production.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc) + _TOKEN_FRESH_THRESHOLD


async def _do_refresh(session: AsyncSession, limits: AccountLimits) -> str | None:
    """Обновляет access_token. При провале — ставит refresh_status=expired, возвращает None."""
    try:
        refreshed = await refresh_access_token(limits.refresh_token_encrypted)
    except RefreshFailedError:
        limits.refresh_status = "expired"
        limits.refresh_failed_at = datetime.now(timezone.utc)
        limits.refresh_recover_attempts += 1
        await session.commit()
        return None

    limits.access_token_encrypted = refreshed.access_token
    limits.refresh_token_encrypted = refreshed.refresh_token
    limits.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    limits.refresh_recover_attempts = 0
    limits.refresh_status = "ok"
    await session.commit()
    return refreshed.access_token
