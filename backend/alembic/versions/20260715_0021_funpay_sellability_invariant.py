"""Normalize tiers unsupported by the FunPay ChatGPT offer form.

Revision ID: 20260715_0021
Revises: 20260714_0020
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260715_0021"
down_revision: str | None = "20260714_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every canonical plan remains available for account recognition. Sale is
    # narrower: the current FunPay form can represent only these six products
    # without mislabeling the buyer's account.
    op.get_bind().execute(
        sa.text(
            "UPDATE subscription_tiers SET is_sellable = false "
            "WHERE system_managed = true AND (code IS NULL OR code NOT IN "
            "('free', 'go', 'plus', 'pro_5x', 'pro_20x', 'business'))"
        )
    )


def downgrade() -> None:
    # The previous value may have been an operator choice, so it cannot be
    # reconstructed safely. A downgrade leaves the conservative flags intact.
    pass
