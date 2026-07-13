from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import math
from datetime import datetime, timezone


MIN_DURATION_MINUTES = 30
MAX_DURATION_MINUTES = 30 * 24 * 60
DURATION_STEP_MINUTES = 30


def format_duration(minutes: int, lang: str) -> str:
    """Return an exact, display-ready localized rental duration.

    The catalog stores one integer unit (minutes).  Formatting is deliberately
    centralized so a 30-minute offer can never be presented as zero days in a
    lot or buyer message.
    """

    if minutes <= 0:
        return "—"
    days, remainder = divmod(minutes, 24 * 60)
    hours, remainder_minutes = divmod(remainder, 60)
    parts: list[str] = []
    if lang == "en":
        if days:
            parts.append(f"{days} {'day' if days == 1 else 'days'}")
        if hours:
            parts.append(f"{hours} {'hour' if hours == 1 else 'hours'}")
        if remainder_minutes:
            parts.append(
                f"{remainder_minutes} "
                f"{'minute' if remainder_minutes == 1 else 'minutes'}"
            )
    else:
        if days:
            parts.append(f"{days} д.")
        if hours:
            parts.append(f"{hours} ч.")
        if remainder_minutes:
            parts.append(f"{remainder_minutes} мин.")
    return " ".join(parts)


def format_legacy_days(minutes: int) -> str:
    """Exact day fraction for deprecated custom templates using ``{days}``.

    New bundled templates use ``{duration}``.  Keeping this flat value avoids
    breaking an operator-edited legacy template while never rounding a
    sub-day offer down to zero.
    """

    days = (Decimal(minutes) / Decimal(24 * 60)).quantize(
        Decimal("0.0000000001"),
        rounding=ROUND_HALF_UP,
    )
    return format(days, "f").rstrip("0").rstrip(".")


def format_remaining_seconds(seconds: float, lang: str) -> str:
    """Compact exact remaining time without rounding the term down."""

    if seconds <= 0:
        return "0"
    total_seconds = math.ceil(seconds)
    days, remainder_seconds = divmod(total_seconds, 24 * 60 * 60)
    hours, remainder_seconds = divmod(remainder_seconds, 60 * 60)
    minutes, remaining_seconds = divmod(remainder_seconds, 60)
    labels = (
        ("d", "h", "min", "sec")
        if lang == "en"
        else ("д", "ч", "мин", "с")
    )
    values = (
        (days, labels[0]),
        (hours, labels[1]),
        (minutes, labels[2]),
        (remaining_seconds, labels[3]),
    )
    parts = [f"{value} {label}" for value, label in values if value]
    return " ".join(parts)


def format_plan_expiry(value: datetime | None, lang: str) -> str:
    if value is None:
        return "No expiry" if lang == "en" else "Без срока"
    return _as_utc(value).strftime("%Y-%m-%d" if lang == "en" else "%d.%m.%Y")


def format_access_expiry(value: datetime, lang: str) -> str:
    value = _as_utc(value)
    if lang == "en":
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return value.strftime("%d.%m.%Y %H:%M UTC")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
