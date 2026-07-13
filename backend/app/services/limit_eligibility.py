from __future__ import annotations

from sqlalchemy import case, or_

from app.models.account import AccountLimits


_FIVE_HOURS_SECONDS = 5 * 60 * 60


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
    """Verified plan-specific long window: Free 30d, every paid plan 7d."""

    return case(
        (
            AccountLimits.codex_primary_window_seconds
            == AccountLimits.expected_long_window_seconds,
            AccountLimits.codex_primary_remaining_pct,
        ),
        (
            AccountLimits.codex_secondary_window_seconds
            == AccountLimits.expected_long_window_seconds,
            AccountLimits.codex_secondary_remaining_pct,
        ),
        else_=AccountLimits.codex_weekly_remaining_pct,
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

    OpenAI names the observed Codex windows ``primary`` and ``secondary``.
    Their duration is data, not a fixed product contract: for example, the
    currently observed Free window is 30 days while paid plans use 7 days.
    The legacy 5h/week columns are used only as a migration fallback for rows
    that have not yet been measured with the exact-window implementation.
    """

    codex_primary = observed_codex_primary()
    codex_secondary = observed_codex_secondary()
    codex_short = observed_codex_short()
    codex_long = observed_codex_long()

    if scope == "any":
        # ``any`` makes no minimum guarantee. Optional ceilings let the seller
        # reserve high-capacity accounts, but only observed windows may be used.
        if max_short_pct is not None:
            statement = statement.where(
                # An explicitly configured short-window ceiling is a real
                # eligibility condition. Plans without an observed 5-hour
                # window (notably Free) must not satisfy it through NULL.
                codex_short <= max_short_pct,
            )
        if max_long_pct is not None:
            statement = statement.where(
                codex_long <= max_long_pct,
            )
        return statement

    if scope == "codex":
        if min_limit_pct is not None:
            statement = statement.where(
                codex_primary >= min_limit_pct,
                or_(
                    codex_secondary.is_(None),
                    codex_secondary >= min_limit_pct,
                ),
            )
        return statement

    # Unknown catalog data must never make an account sellable.
    return statement.where(False)
