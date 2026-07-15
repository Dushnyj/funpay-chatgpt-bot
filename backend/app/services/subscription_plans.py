"""Canonical ChatGPT subscription catalog and conservative plan resolver.

OpenAI exposes plan names through more than one private endpoint.  The values
are not a stable public enum (for example ``prolite`` and the legacy ``team``
name), so the application keeps the raw signals and maps only explicit aliases.
Anything ambiguous remains unknown and therefore cannot be sold accidentally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SubscriptionPlanDefinition:
    code: str
    name: str
    description: str
    aliases: frozenset[str]
    sort_order: int
    usage_multiplier: float | None
    is_sellable: bool = True


@dataclass(frozen=True, slots=True)
class PlanSignal:
    raw: str | None
    source: str
    confidence: float


@dataclass(frozen=True, slots=True)
class ResolvedSubscriptionPlan:
    definition: SubscriptionPlanDefinition | None
    raw: str | None
    source: str | None
    confidence: float
    reason: str

    @property
    def code(self) -> str | None:
        return self.definition.code if self.definition is not None else None

    @property
    def is_sellable(self) -> bool:
        return bool(self.definition and self.definition.is_sellable)


def _clean_raw(raw: str | None) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _normalise_raw(raw: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    for prefix in ("chatgpt_", "chat_gpt_"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    if value.endswith("_plan"):
        value = value[:-5]
    return value


def _aliases(*values: str) -> frozenset[str]:
    return frozenset(_normalise_raw(value) for value in values)


SYSTEM_SUBSCRIPTION_PLANS: tuple[SubscriptionPlanDefinition, ...] = (
    SubscriptionPlanDefinition(
        "free", "Free", "ChatGPT Free", _aliases("free"), 10, None
    ),
    SubscriptionPlanDefinition(
        "go", "Go", "ChatGPT Go", _aliases("go"), 20, None
    ),
    SubscriptionPlanDefinition(
        "plus", "Plus", "ChatGPT Plus", _aliases("plus"), 30, 1.0
    ),
    SubscriptionPlanDefinition(
        "pro_5x",
        "Pro 5x",
        "ChatGPT Pro с профилем лимитов 5x (raw: prolite)",
        _aliases("prolite", "pro_lite", "pro 5x", "pro_5x", "pro5x"),
        40,
        5.0,
    ),
    SubscriptionPlanDefinition(
        "pro_20x",
        "Pro 20x",
        "ChatGPT Pro с профилем лимитов 20x (raw: pro)",
        _aliases("pro", "pro 20x", "pro_20x", "pro20x"),
        50,
        20.0,
    ),
    SubscriptionPlanDefinition(
        "business",
        "Business / usage-based",
        "ChatGPT Business, включая прежнее raw-имя team",
        _aliases(
            "business",
            "team",
            "self_serve_business_usage_based",
            "business usage based",
            "business_usage_based",
        ),
        60,
        None,
    ),
    SubscriptionPlanDefinition(
        "enterprise",
        "Enterprise / usage-based",
        "ChatGPT Enterprise с usage-based конфигурацией",
        _aliases(
            "enterprise",
            "enterprise_cbp_usage_based",
            "enterprise usage based",
            "enterprise_usage_based",
        ),
        70,
        None,
        False,
    ),
    SubscriptionPlanDefinition(
        "edu",
        "Edu",
        "ChatGPT Edu",
        _aliases("edu", "education"),
        80,
        None,
        False,
    ),
    SubscriptionPlanDefinition(
        "teachers",
        "Teachers",
        "ChatGPT for Teachers",
        _aliases("teacher", "teachers"),
        90,
        None,
        False,
    ),
    SubscriptionPlanDefinition(
        "healthcare",
        "Healthcare",
        "ChatGPT for Healthcare",
        _aliases("healthcare", "health care"),
        100,
        None,
        False,
    ),
    SubscriptionPlanDefinition(
        "clinicians",
        "Clinicians",
        "ChatGPT for Clinicians",
        _aliases("clinician", "clinicians"),
        110,
        None,
        False,
    ),
    SubscriptionPlanDefinition(
        "gov",
        "Gov",
        "ChatGPT Gov",
        _aliases("gov", "government"),
        120,
        None,
        False,
    ),
)

PLANS_BY_CODE = {plan.code: plan for plan in SYSTEM_SUBSCRIPTION_PLANS}
FREE_LONG_WINDOW_SECONDS = 30 * 24 * 60 * 60
PAID_LONG_WINDOW_SECONDS = 7 * 24 * 60 * 60
PAID_SHORT_WINDOW_SECONDS = 5 * 60 * 60
_PLANS_BY_ALIAS = {
    alias: plan
    for plan in SYSTEM_SUBSCRIPTION_PLANS
    for alias in plan.aliases
}


def resolve_subscription_plan(
    signals: Iterable[PlanSignal],
) -> ResolvedSubscriptionPlan:
    """Resolve agreeing endpoint signals into one canonical subscription.

    A raw value that is not in the explicit alias table, or two endpoints that
    resolve to different plans, deliberately produces ``unknown``.  Callers
    must not reuse a previously assigned tier in that case.
    """

    meaningful = [signal for signal in signals if _clean_raw(signal.raw)]
    if not meaningful:
        return ResolvedSubscriptionPlan(None, None, None, 0.0, "no_signal")

    resolved: list[tuple[PlanSignal, SubscriptionPlanDefinition | None]] = [
        (signal, plan_for_raw(signal.raw)) for signal in meaningful
    ]
    raw = " | ".join(dict.fromkeys(_clean_raw(signal.raw) for signal in meaningful))
    source = "+".join(dict.fromkeys(signal.source for signal in meaningful))

    if any(plan is None for _, plan in resolved):
        return ResolvedSubscriptionPlan(None, raw, source, 0.0, "unknown_signal")

    definitions = {plan.code: plan for _, plan in resolved if plan is not None}
    if len(definitions) != 1:
        return ResolvedSubscriptionPlan(None, raw, source, 0.0, "conflicting_signals")

    definition = next(iter(definitions.values()))
    confidence = min(
        1.0, max(0.0, min(signal.confidence for signal, _ in resolved))
    )
    if len(resolved) > 1:
        confidence = min(1.0, confidence + 0.02 * (len(resolved) - 1))
    return ResolvedSubscriptionPlan(
        definition,
        raw,
        source,
        round(confidence, 3),
        "resolved",
    )


def plan_for_raw(raw: str | None) -> SubscriptionPlanDefinition | None:
    cleaned = _clean_raw(raw)
    if not cleaned:
        return None
    return _PLANS_BY_ALIAS.get(_normalise_raw(cleaned))


def expected_long_window_seconds(plan_code: str) -> int:
    if plan_code not in PLANS_BY_CODE:
        raise ValueError(f"Unsupported subscription plan code: {plan_code}")
    return (
        FREE_LONG_WINDOW_SECONDS
        if plan_code == "free"
        else PAID_LONG_WINDOW_SECONDS
    )


def validate_plan_window_contract(
    plan_code: str,
    primary_window_seconds: int | None,
    secondary_window_seconds: int | None,
) -> tuple[bool, int]:
    """Validate the observed long Codex window for a resolved plan.

    Paid accounts commonly expose a 5-hour primary window and a 7-day
    secondary window. Free currently exposes the 30-day window as primary.
    The contract therefore searches both exact observed windows instead of
    assigning product meaning to OpenAI's positional names.
    """

    expected = expected_long_window_seconds(plan_code)
    observed = {
        value
        for value in (primary_window_seconds, secondary_window_seconds)
        if value is not None and value > 0
    }
    if expected not in observed:
        return False, expected

    # Treat every reported positive duration as part of the contract instead
    # of accepting an expected duration alongside an unknown second window.
    # Free exposes only its 30-day allowance. Paid plans may expose either the
    # 7-day allowance alone or a separate 5-hour allowance alongside it.
    allowed = (
        {FREE_LONG_WINDOW_SECONDS}
        if plan_code == "free"
        else {PAID_SHORT_WINDOW_SECONDS, PAID_LONG_WINDOW_SECONDS}
    )
    if not observed.issubset(allowed):
        return False, expected
    return True, expected
