"""Expand review state columns for AI lifecycle decisions.

Revision ID: 20261120_03
Revises: 20261120_02
"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_03"
down_revision = "20261120_02"
branch_labels = None
depends_on = None


def _widen_column(table_name: str, column_name: str, length: int) -> None:
    columns = {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }
    if column_name not in columns:
        raise RuntimeError(
            f"Cannot expand AI lifecycle schema: {table_name}.{column_name} is missing."
        )

    column = columns[column_name]
    current_type = column["type"]
    if not isinstance(current_type, sa.String):
        raise TypeError(
            f"Cannot expand AI lifecycle schema: {table_name}.{column_name} "
            f"has unexpected type {current_type!r}."
        )
    if current_type.length is None or current_type.length >= length:
        return

    op.alter_column(
        table_name,
        column_name,
        existing_type=current_type,
        type_=sa.String(length=length),
        existing_nullable=column["nullable"],
    )


def upgrade() -> None:
    _widen_column("reviews", "decision", 40)
    _widen_column("reviews", "lifecycle_state", 40)


def downgrade() -> None:
    raise RuntimeError(
        "This AI lifecycle migration is forward-only. Narrowing state columns "
        "could truncate persisted review decisions."
    )
