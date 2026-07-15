from __future__ import annotations

from sqlalchemy import and_, case, not_

from app.models.account import AccountLimits


_FIVE_HOURS_SECONDS = 5 * 60 * 60
_SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60
_THIRTY_DAYS_SECONDS = 30 * 24 * 60 * 60


def observed_codex_primary():
    """Remaining percentage in the measured primary Codex window."""
    return case(
        (
            AccountLimits.codex_primary_remaining_pct.isnot(None),
            AccountLimits.codex_primary_remaining_pct,
        ),
        else_=AccountLimits.codex_5h_remaining_pct,
    )


def observed_codex_secondary():
    """Optional secondary window from the same measurement generation."""
    return case(
        (
            AccountLimits.codex_primary_remaining_pct.isnot(None),
            AccountLimits.codex_secondary_remaining_pct,
        ),
        else_=AccountLimits.codex_weekly_remaining_pct,
    )


def observed_codex_short():
    """Observed 5-hour window, independent of primary/secondary position."""

    return case(
        (
            AccountLimits.codex_primary_window_seconds == _FIVE_HOURS_SECONDS,
            AccountLimits.codex_primary_remaining_pct,
        ),
        (
            AccountLimits.codex_secondary_window_seconds == _FIVE_HOURS_SECONDS,
            AccountLimits.codex_secondary_remaining_pct,
        ),
        else_=AccountLimits.codex_5h_remaining_pct,
    )


def observed_codex_long():
    """Return the one verified plan-specific long Codex observation.

    ``primary`` and ``secondary`` are provider positions, not product limits.
    A sellable observation must match the duration expected for the resolved
    plan (30 days for Free, 7 days for paid plans), carry a valid percentage,
    and be unambiguous. Legacy weekly/5h aliases deliberately do not qualify.
    """

    expected = AccountLimits.expected_long_window_seconds
    contract_verified = and_(
        AccountLimits.plan_window_status == "ok",
        expected.is_not(None),
        expected.in_((_SEVEN_DAYS_SECONDS, _THIRTY_DAYS_SECONDS)),
    )
    primary_matches = case(
        (
            and_(
                contract_verified,
                AccountLimits.codex_primary_window_seconds.is_not(None),
                AccountLimits.codex_primary_window_seconds == expected,
                AccountLimits.codex_primary_remaining_pct.is_not(None),
                AccountLimits.codex_primary_remaining_pct.between(0, 100),
            ),
            True,
        ),
        else_=False,
    )
    secondary_matches = case(
        (
            and_(
                contract_verified,
                AccountLimits.codex_secondary_window_seconds.is_not(None),
                AccountLimits.codex_secondary_window_seconds == expected,
                AccountLimits.codex_secondary_remaining_pct.is_not(None),
                AccountLimits.codex_secondary_remaining_pct.between(0, 100),
            ),
            True,
        ),
        else_=False,
    )
    return case(
        (
            and_(primary_matches, not_(secondary_matches)),
            AccountLimits.codex_primary_remaining_pct,
        ),
        (
            and_(secondary_matches, not_(primary_matches)),
            AccountLimits.codex_secondary_remaining_pct,
        ),
        else_=None,
    )


def apply_limit_scope_filters(
    statement,
    *,
    scope: str,
    min_limit_pct: int | None,
    max_short_pct: int | None,
    max_long_pct: int | None,
):
    """Apply one shared set of allocation and lot-capacity predicates.

    Only the verified plan-specific long observation participates in sales:
    30 days for Free and 7 days for paid plans. The positional provider names
    and legacy 5h/weekly fields remain storage/API compatibility details.
    """

    codex_long = observed_codex_long()

    if scope == "any":
        # ``max_short_pct`` is accepted only for legacy request compatibility.
        # It cannot influence new allocations because the product exposes and
        # sells one long Codex allowance.
        statement = statement.where(codex_long.is_not(None))
        if max_long_pct is not None:
            statement = statement.where(
                codex_long <= max_long_pct,
            )
        return statement

    if scope == "codex":
        if min_limit_pct is None:
            return statement.where(False)
        return statement.where(codex_long >= min_limit_pct)

    # Unknown catalog data must never make an account sellable.
    return statement.where(False)
