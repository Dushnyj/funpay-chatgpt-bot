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

    workspace_id: str | None = None
    plan_type: str | None = None
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
        return cls(
            workspace_id=account_id,
            plan_type=(
                account.get("plan_type")
                or entitlement.get("plan_type")
                or entry.get("plan_type")
            ),
            subscription_expires_at=_parse_dt(entitlement.get("expires_at")),
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


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
