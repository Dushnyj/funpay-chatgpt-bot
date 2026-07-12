from app.api.schemas import (
    AccountOut, AccountCreate, TierOut, DurationOut,
    LotOut, OrderOut, RentalOut, SettingsOut, SettingsUpdate,
    MetricsOut, PriceMatrixItem, TemplateOut,
)


def test_account_out_excludes_secrets():
    """AccountOut НЕ содержит password_encrypted/totp_secret_encrypted."""
    fields = AccountOut.model_fields
    assert "password_encrypted" not in fields
    assert "totp_secret_encrypted" not in fields


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
