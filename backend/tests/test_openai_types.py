from datetime import datetime, timezone

from app.integrations.openai.types import AccountMetadata, UsageInfo


def test_usage_info_from_api_response():
    """Парсинг ответа /wham/usage с обоими окнами."""
    raw = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 18,
                "limit_window_seconds": 18000,
                "reset_at": "2026-07-12T18:00:00Z",
            },
            "secondary_window": {
                "used_percent": 33,
                "limit_window_seconds": 604800,
                "reset_at": "2026-07-14T00:00:00Z",
            },
        },
    }
    info = UsageInfo.from_api_response(raw)
    assert info.plan_type == "plus"
    assert info.primary_remaining_pct == 82  # 100 - 18
    assert info.secondary_remaining_pct == 67  # 100 - 33
    assert info.primary_window_seconds == 18000
    assert info.secondary_window_seconds == 604800
    assert info.primary_resets_at == datetime(2026, 7, 12, 18, tzinfo=timezone.utc)
    assert info.secondary_resets_at == datetime(2026, 7, 14, tzinfo=timezone.utc)


def test_usage_info_supports_unix_reset_and_nonstandard_single_window():
    raw = {
        "plan_type": "free",
        "rate_limit": {
            "primary_window": {
                "used_percent": 7,
                "limit_window_seconds": 2592000,
                "reset_at": 1783987200,
            },
        },
    }

    info = UsageInfo.from_api_response(raw)

    assert info.primary_remaining_pct == 93
    assert info.primary_window_seconds == 2592000
    assert info.primary_resets_at == datetime.fromtimestamp(
        1783987200, tz=timezone.utc
    )
    assert info.secondary_remaining_pct is None
    assert info.secondary_window_seconds is None
    assert info.secondary_resets_at is None


def test_usage_info_handles_missing_windows():
    """Ответ без rate_limit — все pct = None."""
    raw = {"plan_type": "free", "rate_limit": None}
    info = UsageInfo.from_api_response(raw)
    assert info.plan_type == "free"
    assert info.primary_remaining_pct is None
    assert info.secondary_remaining_pct is None


def test_account_metadata_from_accounts_check():
    raw = {
        "accounts": {
            "acc-plus": {
                "account": {"plan_type": "plus"},
                "entitlement": {
                    "has_active_subscription": True,
                    "expires_at": "2026-08-15T00:00:00Z",
                },
            }
        }
    }
    meta = AccountMetadata.from_accounts_check(raw, account_id="acc-plus")
    assert meta.workspace_id == "acc-plus"
    assert meta.plan_type == "plus"
    assert meta.has_active_subscription is True
    assert meta.subscription_expires_at == datetime(2026, 8, 15, tzinfo=timezone.utc)


def test_account_metadata_ignores_stale_expiry_for_inactive_entitlement():
    raw = {
        "accounts": {
            "acc-free": {
                "account": {"plan_type": "free"},
                "entitlement": {
                    "subscription_plan": "chatgptplusplan",
                    "has_active_subscription": False,
                    "expires_at": 1700000000,
                },
            }
        }
    }

    meta = AccountMetadata.from_accounts_check(raw, account_id="acc-free")

    assert meta.workspace_id == "acc-free"
    assert meta.plan_type == "free"
    assert meta.has_active_subscription is False
    assert meta.subscription_expires_at is None


def test_account_metadata_parses_unix_expiry_for_active_entitlement():
    raw = {
        "accounts": {
            "acc-plus": {
                "account": {"plan_type": "plus"},
                "entitlement": {
                    "has_active_subscription": True,
                    "expires_at": 1783987200.0,
                },
            }
        }
    }

    meta = AccountMetadata.from_accounts_check(raw, account_id="acc-plus")

    assert meta.subscription_expires_at == datetime.fromtimestamp(
        1783987200.0, tz=timezone.utc
    )


def test_account_metadata_selects_requested_workspace_not_default_or_first():
    raw = {
        "accounts": {
            "default": {
                "account": {"account_id": "acc-other", "plan_type": "free"},
                "entitlement": {"expires_at": None},
            },
            "acc-target": {
                "account": {"plan_type": "pro"},
                "entitlement": {"expires_at": None},
            },
        }
    }
    meta = AccountMetadata.from_accounts_check(raw, account_id="acc-target")
    assert meta.plan_type == "pro"
    assert meta.workspace_id == "acc-target"
    assert meta.subscription_expires_at is None


def test_account_metadata_does_not_fallback_when_workspace_is_missing():
    raw = {
        "accounts": {
            "default": {
                "account": {"plan_type": "plus"},
                "entitlement": {"expires_at": None},
            }
        }
    }
    meta = AccountMetadata.from_accounts_check(raw, account_id="acc-missing")
    assert meta.workspace_id is None
    assert meta.plan_type is None
