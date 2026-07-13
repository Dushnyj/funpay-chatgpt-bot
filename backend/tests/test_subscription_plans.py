import pytest

from app.services.subscription_plans import (
    SYSTEM_SUBSCRIPTION_PLANS,
    PlanSignal,
    expected_long_window_seconds,
    plan_for_raw,
    resolve_subscription_plan,
    validate_plan_window_contract,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("free", "free"),
        ("go", "go"),
        ("plus", "plus"),
        ("prolite", "pro_5x"),
        ("pro", "pro_20x"),
        ("team", "business"),
        ("self_serve_business_usage_based", "business"),
        ("business_usage_based", "business"),
        ("enterprise_cbp_usage_based", "enterprise"),
        ("enterprise-usage-based", "enterprise"),
        ("education", "edu"),
        ("teacher", "teachers"),
        ("health care", "healthcare"),
        ("clinician", "clinicians"),
        ("government", "gov"),
    ),
)
def test_raw_aliases_resolve_to_canonical_plan(raw, expected):
    assert plan_for_raw(raw).code == expected


def test_catalog_contains_every_supported_plan_and_pro_multipliers():
    by_code = {plan.code: plan for plan in SYSTEM_SUBSCRIPTION_PLANS}
    assert set(by_code) == {
        "free", "go", "plus", "pro_5x", "pro_20x", "business",
        "enterprise", "edu", "teachers", "healthcare", "clinicians", "gov",
    }
    assert by_code["pro_5x"].usage_multiplier == 5.0
    assert by_code["pro_20x"].usage_multiplier == 20.0
    assert by_code["free"].usage_multiplier is None
    assert by_code["go"].usage_multiplier is None


def test_agreeing_aliases_resolve_with_auditable_evidence():
    result = resolve_subscription_plan(
        (
            PlanSignal("team", "accounts_check", 0.98),
            PlanSignal("business", "wham_usage", 0.90),
        )
    )
    assert result.code == "business"
    assert result.raw == "team | business"
    assert result.source == "accounts_check+wham_usage"
    assert result.confidence == pytest.approx(0.92)
    assert result.is_sellable is True


def test_conflicting_or_unknown_signal_is_never_sellable():
    conflict = resolve_subscription_plan(
        (
            PlanSignal("plus", "accounts_check", 0.98),
            PlanSignal("pro", "wham_usage", 0.90),
        )
    )
    unknown = resolve_subscription_plan(
        (PlanSignal("future-super-plan", "accounts_check", 0.98),)
    )
    assert conflict.code is None
    assert conflict.reason == "conflicting_signals"
    assert conflict.is_sellable is False
    assert unknown.code is None
    assert unknown.reason == "unknown_signal"
    assert unknown.is_sellable is False


def test_free_contract_is_exactly_thirty_day_long_window():
    assert expected_long_window_seconds("free") == 30 * 24 * 60 * 60
    assert validate_plan_window_contract(
        "free", 30 * 24 * 60 * 60, None,
    ) == (True, 30 * 24 * 60 * 60)
    assert validate_plan_window_contract(
        "free", 7 * 24 * 60 * 60, None,
    ) == (False, 30 * 24 * 60 * 60)
    assert validate_plan_window_contract(
        "free", 30 * 24 * 60 * 60, 7 * 24 * 60 * 60,
    ) == (False, 30 * 24 * 60 * 60)
    assert validate_plan_window_contract(
        "free", 30 * 24 * 60 * 60, 5 * 60 * 60,
    ) == (False, 30 * 24 * 60 * 60)


def test_unknown_plan_never_inherits_paid_window_contract():
    with pytest.raises(ValueError, match="Unsupported subscription plan"):
        expected_long_window_seconds("future-super-plan")


@pytest.mark.parametrize(
    "plan_code",
    [plan.code for plan in SYSTEM_SUBSCRIPTION_PLANS if plan.code != "free"],
)
def test_every_paid_contract_has_five_hour_plus_seven_day_windows(plan_code):
    assert expected_long_window_seconds(plan_code) == 7 * 24 * 60 * 60
    assert validate_plan_window_contract(
        plan_code,
        5 * 60 * 60,
        7 * 24 * 60 * 60,
    ) == (True, 7 * 24 * 60 * 60)
    assert validate_plan_window_contract(
        plan_code,
        5 * 60 * 60,
        30 * 24 * 60 * 60,
    ) == (False, 7 * 24 * 60 * 60)
    assert validate_plan_window_contract(
        plan_code,
        7 * 24 * 60 * 60,
        None,
    ) == (True, 7 * 24 * 60 * 60)
    assert validate_plan_window_contract(
        plan_code,
        7 * 24 * 60 * 60,
        14 * 24 * 60 * 60,
    ) == (False, 7 * 24 * 60 * 60)
