import pytest
from pydantic import ValidationError

from app.api.schemas import (
    AccountOut, AccountCreate, AccountUpdate, TierOut, DurationOut,
    LotOut, OrderOut, RentalOut, SettingsOut, SettingsUpdate,
    MetricsOut, PriceMatrixItem, TemplateOut,
)


def test_account_out_excludes_secrets():
    """AccountOut НЕ содержит password_encrypted/totp_secret_encrypted."""
    fields = AccountOut.model_fields
    assert "password_encrypted" not in fields
    assert "totp_secret_encrypted" not in fields


def test_account_write_schemas_reject_manual_subscription_expiry():
    assert "subscription_expires_at" not in AccountCreate.model_fields
    assert "subscription_expires_at" not in AccountUpdate.model_fields
    with pytest.raises(ValidationError):
        AccountCreate.model_validate({
            "login": "account@example.com",
            "password": "password",
            "subscription_expires_at": "2026-08-01T00:00:00Z",
        })
    with pytest.raises(ValidationError):
        AccountUpdate.model_validate({
            "subscription_expires_at": "2026-08-01T00:00:00Z",
        })


def test_tier_out():
    t = TierOut(id=1, name="Plus", description=None, is_active=True)
    assert t.name == "Plus"


def test_metrics_out():
    m = MetricsOut(
        active_rentals=5, available_accounts=3, orders_today=2,
        revenue_brutto=1000, revenue_netto=850, bot_status="connected",
    )
    assert m.active_rentals == 5


def test_settings_out_excludes_admin_hash():
    fields = SettingsOut.model_fields
    assert "admin_password_hash" not in fields
    assert "funpay_session_key" not in fields
    assert "telegram_bot_token" not in fields


def test_rental_out_exposes_complete_issued_limit_snapshot():
    assert {
        "issued_codex_primary_pct",
        "issued_codex_primary_window_seconds",
        "issued_codex_primary_resets_at",
        "issued_codex_secondary_pct",
        "issued_codex_secondary_window_seconds",
        "issued_codex_secondary_resets_at",
        "issued_plan_window_status",
        "issued_expected_long_window_seconds",
        "issued_limits_measured_at",
    } <= set(RentalOut.model_fields)
