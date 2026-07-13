from datetime import datetime, timezone

from pydantic import BaseModel


class UsageInfo(BaseModel):
    """Результат замера лимитов из /wham/usage."""

    plan_type: str | None = None
    primary_remaining_pct: int | None = None
    secondary_remaining_pct: int | None = None
    primary_window_seconds: int | None = None
    secondary_window_seconds: int | None = None
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
            primary_window_seconds=_parse_window_seconds(
                primary.get("limit_window_seconds")
            ),
            secondary_window_seconds=_parse_window_seconds(
                secondary.get("limit_window_seconds")
            ),
            primary_resets_at=_parse_dt(primary.get("reset_at")),
            secondary_resets_at=_parse_dt(secondary.get("reset_at")),
        )


class AccountMetadata(BaseModel):
    """Метаданные аккаунта из /accounts/check."""

    workspace_id: str | None = None
    plan_type: str | None = None
    has_active_subscription: bool | None = None
    subscription_expires_at: datetime | None = None

    @classmethod
    def from_accounts_check(
        cls, raw: dict, *, account_id: str | None
    ) -> "AccountMetadata":
        """Parse only the workspace that belongs to ``account_id``.

        The endpoint may return several workspaces and a ``default`` alias.
        Falling back to that alias or to insertion order can silently attribute
        another workspace's subscription to the account being validated.
        """
        accounts = raw.get("accounts") or {}
        entry = _find_account_entry(accounts, account_id)
        if entry is None:
            return cls()
        account = entry.get("account") or {}
        entitlement = entry.get("entitlement") or {}
        active = entitlement.get("has_active_subscription")
        if not isinstance(active, bool):
            active = None
        return cls(
            workspace_id=account_id,
            plan_type=(
                account.get("plan_type")
                or entitlement.get("plan_type")
                or entry.get("plan_type")
            ),
            has_active_subscription=active,
            # ``expires_at`` may remain populated long after an entitlement
            # became inactive. It is a current subscription expiry only when
            # the endpoint explicitly marks the entitlement active.
            subscription_expires_at=(
                _parse_dt(entitlement.get("expires_at")) if active is True else None
            ),
        )


def _find_account_entry(
    accounts: object, account_id: str | None
) -> dict | None:
    if not account_id or not isinstance(accounts, dict):
        return None

    direct = accounts.get(account_id)
    if isinstance(direct, dict):
        return direct

    # Some responses use the literal key "default", but carry the real ID in
    # the nested object.  Matching the nested value is safe; matching the key
    # "default" alone is not.
    for entry in accounts.values():
        if not isinstance(entry, dict):
            continue
        account = entry.get("account")
        nested_ids = (
            entry.get("account_id"),
            entry.get("id"),
            account.get("account_id") if isinstance(account, dict) else None,
            account.get("id") if isinstance(account, dict) else None,
        )
        if account_id in nested_ids:
            return entry
    return None


def _parse_dt(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        return None
    return None


def _parse_window_seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or not float(value).is_integer():
        return None
    return int(value)
