from app.services.durations import (
    format_duration,
    format_legacy_days,
    format_remaining_seconds,
)


def test_format_duration_preserves_sub_day_units():
    assert format_duration(30, "ru") == "30 мин."
    assert format_duration(90, "en") == "1 hour 30 minutes"
    assert format_duration(25 * 60, "ru") == "1 д. 1 ч."


def test_format_remaining_is_compact_and_does_not_round_down():
    assert format_remaining_seconds(30 * 60 - 0.1, "ru") == "30 мин"
    assert format_remaining_seconds(25 * 60 * 60, "ru") == "1 д 1 ч"
    assert format_remaining_seconds(25 * 60 * 60 + 30 * 60, "ru") == (
        "1 д 1 ч 30 мин"
    )
    assert format_remaining_seconds(3_601, "ru") == "1 ч 1 с"
    assert format_remaining_seconds(60.1, "en") == "1 min 1 sec"
    assert format_remaining_seconds(90, "ru") == "1 мин 30 с"
    assert format_remaining_seconds(59.1, "en") == "1 min"


def test_legacy_day_fraction_is_exact_and_compact():
    assert format_legacy_days(30) == "0.0208333333"
    assert format_legacy_days(150) == "0.1041666667"
    assert format_legacy_days(24 * 60) == "1"
    assert format_legacy_days(12 * 60) == "0.5"
