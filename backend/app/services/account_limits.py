import enum
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openai.client import OpenAIClient
from app.integrations.openai.exceptions import BackendApiError, RefreshFailedError, TokenExpiredError
from app.integrations.openai.oauth import parse_id_token, refresh_access_token
from app.integrations.openai.types import AccountMetadata, UsageInfo
from app.integrations.playwright.proxy import BrowserProxy, ProxyUnavailableError
from app.models.account import (
    Account,
    AccountLimits,
    TRUSTED_SUBSCRIPTION_EXPIRY_SOURCES,
)
from app.models.catalog import SubscriptionTier
from app.services.subscription_plans import (
    PLANS_BY_CODE,
    PlanSignal,
    ResolvedSubscriptionPlan,
    resolve_subscription_plan,
    validate_plan_window_contract,
)
from app.services.proxy_routes import mark_proxy_route_offline, resolve_browser_proxy

# access_token считается свежим, если истекает не раньше чем через это время
_TOKEN_FRESH_THRESHOLD = timedelta(minutes=5)
# Скв: при 401 от backend-api делаем refresh и ретраим замер один раз
_MAX_RETRIES = 1
_FIVE_HOURS_SECONDS = 5 * 60 * 60
_ONE_WEEK_SECONDS = 7 * 24 * 60 * 60
_UNRESOLVED_PROXY = object()


class MeasureResult(enum.Enum):
    OK = "ok"
    REFRESH_FAILED = "refresh_failed"
    BACKEND_ERROR = "backend_error"
    PLAN_DETECTION_FAILED = "plan_detection_failed"
    PLAN_WINDOW_MISMATCH = "plan_window_mismatch"


async def measure_account_limits(
    session: AsyncSession,
    account_id: int,
    *,
    claim_plan_type: str | None = None,
    browser_proxy: BrowserProxy | None | object = _UNRESOLVED_PROXY,
) -> MeasureResult:
    """Замеряет лимиты и подписку аккаунта, обновляет AccountLimits.

    Цикл: refresh access_token (если протух) → get_usage + get_account_metadata → запись в БД.
    При RefreshFailedError → refresh_status=expired, возврат REFRESH_FAILED.
    """
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account not found for account_id={account_id}")
    if browser_proxy is _UNRESOLVED_PROXY:
        pinned_proxy = await resolve_browser_proxy(session, account)
    else:
        assert browser_proxy is None or isinstance(browser_proxy, BrowserProxy)
        pinned_proxy = browser_proxy

    limits, access_token = await _acquire_access_token(
        session,
        account_id,
        browser_proxy=pinned_proxy,
    )
    if access_token is None:
        return MeasureResult.REFRESH_FAILED

    # Current OpenAI access tokens carry the ChatGPT account ID and plan in the
    # auth namespace. Older id_token-derived values remain a supported fallback.
    access_claims = parse_id_token(access_token)
    if access_claims.account_id:
        limits.account_id_openai = access_claims.account_id

    # Замер с retry при 401
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with OpenAIClient(
                access_token,
                limits.account_id_openai,
                proxy=pinned_proxy,
            ) as client:
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
            limits, refreshed = await _acquire_access_token(
                session,
                account_id,
                force=True,
                stale_access_token=access_token,
                browser_proxy=pinned_proxy,
            )
            if refreshed is None:
                return MeasureResult.REFRESH_FAILED
            access_token = refreshed
            access_claims = parse_id_token(access_token)
            if access_claims.account_id:
                limits.account_id_openai = access_claims.account_id
        except ProxyUnavailableError:
            await mark_proxy_route_offline(session, pinned_proxy)
            await session.commit()
            raise
        except BackendApiError:
            return MeasureResult.BACKEND_ERROR

    # ``wham/usage`` is the observed common agentic allowance used by Codex,
    # Work, Workspace Agents and related clients. OpenAI may return multiple
    # real windows; preserve each observation instead of inventing a separate
    # Chat allowance that the API does not expose.
    limits.codex_primary_remaining_pct = usage.primary_remaining_pct
    limits.codex_primary_window_seconds = usage.primary_window_seconds
    limits.codex_primary_resets_at = usage.primary_resets_at
    limits.codex_secondary_remaining_pct = usage.secondary_remaining_pct
    limits.codex_secondary_window_seconds = usage.secondary_window_seconds
    limits.codex_secondary_resets_at = usage.secondary_resets_at

    # Backward-compatible aliases must not mislabel a 30-day (or any other)
    # observed window as 5h/weekly.
    limits.codex_5h_remaining_pct = _remaining_for_window(
        usage, _FIVE_HOURS_SECONDS,
    )
    limits.codex_weekly_remaining_pct = _remaining_for_window(
        usage, _ONE_WEEK_SECONDS,
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
    window_contract_ok = True
    plan_detection_ok = resolved_plan.code is not None
    if resolved_plan.code is None:
        limits.plan_window_status = "unknown"
        limits.expected_long_window_seconds = None
    else:
        window_contract_ok, expected_window = validate_plan_window_contract(
            resolved_plan.code,
            usage.primary_window_seconds,
            usage.secondary_window_seconds,
        )
        limits.expected_long_window_seconds = expected_window
        limits.plan_window_status = "ok" if window_contract_ok else "mismatch"
    if resolved_plan.code == "free" or metadata.has_active_subscription is False:
        # Free has no subscription term. An explicitly inactive paid
        # entitlement may carry a historical ``expires_at`` and must fail
        # closed as well.
        _clear_subscription_expiry(account, limits)
    elif (
        metadata.has_active_subscription is True
        and metadata.subscription_expires_at is not None
    ):
        _store_subscription_expiry(
            account,
            limits,
            metadata.subscription_expires_at,
            source="accounts_check",
        )
    else:
        # ``accounts/check`` legitimately omits expiry for some responses.
        # Preserve a prior OpenAI attestation, but never promote the legacy
        # operator-editable date that existed before provenance was stored.
        _synchronize_trusted_subscription_expiry(account, limits)
    limits.measured_at = datetime.now(timezone.utc)
    limits.refresh_status = "ok"
    limits.refresh_failed_at = None

    await session.commit()
    if not plan_detection_ok:
        return MeasureResult.PLAN_DETECTION_FAILED
    if not window_contract_ok:
        return MeasureResult.PLAN_WINDOW_MISMATCH
    return MeasureResult.OK


def _remaining_for_window(
    usage: UsageInfo,
    window_seconds: int,
) -> int | None:
    """Find a compatibility alias by duration, never by array position."""

    if usage.primary_window_seconds == window_seconds:
        return usage.primary_remaining_pct
    if usage.secondary_window_seconds == window_seconds:
        return usage.secondary_remaining_pct
    return None


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


def _store_subscription_expiry(
    account: Account,
    limits: AccountLimits,
    expires_at: datetime,
    *,
    source: str,
) -> None:
    if source not in TRUSTED_SUBSCRIPTION_EXPIRY_SOURCES:
        raise ValueError(f"Untrusted subscription expiry source: {source}")
    account.subscription_expires_at = expires_at
    account.subscription_expiry_source = source
    limits.subscription_expires_at = expires_at
    limits.subscription_expiry_source = source


def _clear_subscription_expiry(
    account: Account,
    limits: AccountLimits,
) -> None:
    account.subscription_expires_at = None
    account.subscription_expiry_source = None
    limits.subscription_expires_at = None
    limits.subscription_expiry_source = None


def _synchronize_trusted_subscription_expiry(
    account: Account,
    limits: AccountLimits,
) -> None:
    candidates = (
        (
            account.subscription_expiry_source,
            account.subscription_expires_at,
        ),
        (
            limits.subscription_expiry_source,
            limits.subscription_expires_at,
        ),
    )
    trusted = [
        (source, expires_at)
        for source, expires_at in candidates
        if source in TRUSTED_SUBSCRIPTION_EXPIRY_SOURCES
        and expires_at is not None
    ]
    if not trusted:
        _clear_subscription_expiry(account, limits)
        return

    # accounts/check is the authoritative entitlement endpoint. Prefer it if
    # the two durable copies were interrupted between commits; otherwise the
    # current ID-token claim remains a valid conservative fallback.
    source, expires_at = next(
        (
            candidate
            for candidate in trusted
            if candidate[0] == "accounts_check"
        ),
        trusted[0],
    )
    _store_subscription_expiry(
        account,
        limits,
        expires_at,
        source=source,
    )


def _is_token_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return True
    # SQLite used in tests drops timezone information; interpret a naive value
    # as UTC, which is also how token expiry is persisted in production.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc) + _TOKEN_FRESH_THRESHOLD


async def _acquire_access_token(
    session: AsyncSession,
    account_id: int,
    *,
    force: bool = False,
    stale_access_token: str | None = None,
    browser_proxy: BrowserProxy | None | object = _UNRESOLVED_PROXY,
) -> tuple[AccountLimits, str | None]:
    """Serialize rotating refresh-token use across workers for one account."""

    limits = (
        await session.execute(
            select(AccountLimits)
            .where(AccountLimits.account_id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if limits is None:
        raise ValueError(f"AccountLimits not found for account_id={account_id}")

    current = limits.access_token_encrypted
    another_worker_refreshed = (
        stale_access_token is not None
        and current is not None
        and current != stale_access_token
        and not _is_token_expired(limits.access_token_expires_at)
    )
    if another_worker_refreshed or (
        not force
        and current is not None
        and not _is_token_expired(limits.access_token_expires_at)
    ):
        # Even the no-refresh path must commit here: this transaction owns the
        # AccountLimits row lock and usage HTTP must run without it.
        await session.commit()
        return limits, current

    if browser_proxy is _UNRESOLVED_PROXY:
        account = await session.get(Account, account_id)
        if account is None:
            raise ValueError(f"Account not found for account_id={account_id}")
        pinned_proxy = await resolve_browser_proxy(session, account)
    else:
        assert browser_proxy is None or isinstance(browser_proxy, BrowserProxy)
        pinned_proxy = browser_proxy
    return limits, await _do_refresh(
        session,
        limits,
        browser_proxy=pinned_proxy,
    )


async def _do_refresh(
    session: AsyncSession,
    limits: AccountLimits,
    *,
    browser_proxy: BrowserProxy | None = None,
) -> str | None:
    """Обновляет access_token. При провале — ставит refresh_status=expired, возвращает None."""
    try:
        if browser_proxy is None:
            refreshed = await refresh_access_token(limits.refresh_token_encrypted)
        else:
            refreshed = await refresh_access_token(
                limits.refresh_token_encrypted,
                proxy=browser_proxy,
            )
    except ProxyUnavailableError:
        await mark_proxy_route_offline(session, browser_proxy)
        await session.commit()
        raise
    except RefreshFailedError:
        limits.refresh_status = "expired"
        limits.refresh_failed_at = datetime.now(timezone.utc)
        await session.commit()
        return None

    limits.access_token_encrypted = refreshed.access_token
    limits.refresh_token_encrypted = refreshed.refresh_token
    limits.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    limits.refresh_recover_attempts = 0
    limits.refresh_status = "ok"
    if refreshed.id_token:
        claims = parse_id_token(refreshed.id_token)
        if claims.subscription_expires_at is not None:
            account = await session.get(Account, limits.account_id)
            if account is not None:
                _store_subscription_expiry(
                    account,
                    limits,
                    claims.subscription_expires_at,
                    source="id_token",
                )
    await session.commit()
    return refreshed.access_token
