"""Normalize encrypted columns and encrypt legacy settings secrets.

Revision ID: 20260713_0003
Revises: 20260713_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken

from alembic import op
from app.config import get_settings


revision: str = "20260713_0003"
down_revision: str | None = "20260713_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize_value(
    fernet: Fernet,
    value: str,
    *,
    allow_legacy_plaintext: bool,
    location: str,
) -> str:
    current = value.encode()
    try:
        inner = fernet.decrypt(current)
    except InvalidToken as exc:
        if allow_legacy_plaintext and not value.startswith("gAAAA"):
            return fernet.encrypt(current).decode()
        raise RuntimeError(
            f"Cannot decrypt {location}; verify ENCRYPTION_KEY before migration"
        ) from exc

    normalized = current
    while True:
        try:
            next_inner = fernet.decrypt(inner)
        except InvalidToken:
            break
        normalized = inner
        inner = next_inner
    return normalized.decode()


def _normalize_table(
    table_name: str,
    id_column: str,
    encrypted_columns: tuple[str, ...],
    *,
    allow_legacy_plaintext: bool = False,
) -> None:
    bind = op.get_bind()
    table = sa.table(
        table_name,
        sa.column(id_column),
        *(sa.column(column, sa.String()) for column in encrypted_columns),
    )
    fernet = Fernet(get_settings().encryption_key.encode())
    for row in bind.execute(sa.select(table)).mappings():
        updates: dict[str, str] = {}
        for column in encrypted_columns:
            value = row[column]
            if value is None:
                continue
            normalized = _normalize_value(
                fernet,
                value,
                allow_legacy_plaintext=allow_legacy_plaintext,
                location=f"{table_name}.{column} row {row[id_column]}",
            )
            if normalized != value:
                updates[column] = normalized
        if updates:
            bind.execute(
                table.update().where(table.c[id_column] == row[id_column]).values(**updates)
            )


def upgrade() -> None:
    _normalize_table(
        "accounts",
        "id",
        (
            "password_encrypted",
            "totp_secret_encrypted",
            "email_password_encrypted",
        ),
    )
    _normalize_table(
        "account_limits",
        "account_id",
        ("refresh_token_encrypted", "access_token_encrypted"),
    )
    _normalize_table(
        "seller_settings",
        "id",
        ("funpay_session_key", "telegram_bot_token"),
        allow_legacy_plaintext=True,
    )


def downgrade() -> None:
    # Encryption normalization is deliberately irreversible.
    pass
