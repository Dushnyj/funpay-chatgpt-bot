from datetime import datetime

from pydantic import BaseModel


class UsageInfo(BaseModel):
    """Результат замера лимитов из /wham/usage."""

    plan_type: str | None = None
    primary_remaining_pct: int | None = None  # 5h окно
    secondary_remaining_pct: int | None = None  # weekly окно
    primary_resets_at: datetime | None = None
    secondary_resets_at: datetime | None = None

    @classmethod
    def from_api_response(cls, raw: dict) -> "UsageInfo":
        rate_limit = raw.get("rate_limit") or {}
        primary = rate_limit.get("primary_window") or {}
        secondary = rate_limit.get("secondary_window") or {}

        primary_used = primary.get("used_percent")
        secondary_used = secondary.get("used_percent")

        return cls(
            plan_type=raw.get("plan_type"),
            primary_remaining_pct=(100 - primary_used) if primary_used is not None else None,
            secondary_remaining_pct=(100 - secondary_used) if secondary_used is not None else None,
            primary_resets_at=_parse_dt(primary.get("reset_at")),
            secondary_resets_at=_parse_dt(secondary.get("reset_at")),
        )


class AccountMetadata(BaseModel):
    """Метаданные аккаунта из /accounts/check."""

    plan_type: str | None = None
    subscription_expires_at: datetime | None = None

    @classmethod
    def from_accounts_check(cls, raw: dict) -> "AccountMetadata":
        accounts = raw.get("accounts") or {}
        entry = accounts.get("default") or next(iter(accounts.values()), None)
        if entry is None:
            return cls()
        account = entry.get("account") or {}
        entitlement = entry.get("entitlement") or {}
        return cls(
            plan_type=account.get("plan_type"),
            subscription_expires_at=_parse_dt(entitlement.get("expires_at")),
        )


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
