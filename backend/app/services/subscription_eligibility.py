from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_

from app.models.account import (
    Account,
    TRUSTED_SUBSCRIPTION_EXPIRY_SOURCES,
)


def trusted_paid_subscription_expiry(required_until: datetime):
    """SQL predicate for an OpenAI-attested paid subscription deadline."""

    return and_(
        Account.subscription_expiry_source.in_(
            TRUSTED_SUBSCRIPTION_EXPIRY_SOURCES
        ),
        Account.subscription_expires_at >= required_until,
    )
