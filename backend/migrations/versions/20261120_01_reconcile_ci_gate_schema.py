"""Reconcile the CI-gated review schema with the SQLAlchemy models.

Revision ID: 20261120_01
Revises:
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_01"
down_revision = None
branch_labels = None
depends_on = None


def _columns(table_name: str) -> dict[str, sa.Column]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        raise RuntimeError(
            f"Cannot apply CI-gated schema migration: table {table_name!r} is missing."
        )
    return {column["name"]: column for column in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing(
        "pull_requests",
        sa.Column("merged_at", sa.DateTime(), nullable=True),
    )
    _add_column_if_missing(
        "pull_requests",
        sa.Column("merge_commit_sha", sa.String(length=64), nullable=True),
    )

    _add_column_if_missing(
        "reviews",
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'RECEIVED'"),
        ),
    )
    _add_column_if_missing(
        "reviews",
        sa.Column("ci_verified_at", sa.DateTime(), nullable=True),
    )

    _add_column_if_missing(
        "ci_revisions",
        sa.Column(
            "required_checks",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    _add_column_if_missing(
        "ci_revisions",
        sa.Column(
            "discovery_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    _add_column_if_missing(
        "ci_revisions",
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
    )
    _add_column_if_missing(
        "ci_executions",
        sa.Column("app_id", sa.String(length=64), nullable=True),
    )

    publication_columns = _columns("review_publications")
    message_type = publication_columns["message_type"]["type"]
    if (
        isinstance(message_type, sa.String)
        and message_type.length is not None
        and message_type.length < 64
    ):
        op.alter_column(
            "review_publications",
            "message_type",
            existing_type=message_type,
            type_=sa.String(length=64),
            existing_nullable=False,
        )


def downgrade() -> None:
    raise RuntimeError(
        "This schema-reconciliation migration is forward-only. "
        "Dropping reconciled columns could destroy production data."
    )
