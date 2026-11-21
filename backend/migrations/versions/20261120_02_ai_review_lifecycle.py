"""Persist AI review lifecycle states and the unavailable decision.

Revision ID: 20261120_02
Revises: 20261120_01
"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_02"
down_revision = "20261120_01"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        raise RuntimeError(
            f"Cannot apply AI lifecycle migration: table {table_name!r} is missing."
        )
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing(
        "pull_requests",
        sa.Column(
            "ai_state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'AI_UNAVAILABLE'"),
        ),
    )
    _add_column_if_missing(
        "pull_requests", sa.Column("ai_error_code", sa.String(length=64), nullable=True)
    )
    _add_column_if_missing(
        "pull_requests", sa.Column("ai_retryable_at", sa.DateTime(), nullable=True)
    )
    _add_column_if_missing(
        "pull_requests", sa.Column("ai_attempt_id", sa.String(length=36), nullable=True)
    )
    _add_column_if_missing(
        "reviews",
        sa.Column(
            "ai_state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'AI_UNAVAILABLE'"),
        ),
    )
    _add_column_if_missing(
        "reviews", sa.Column("ai_error_code", sa.String(length=64), nullable=True)
    )
    _add_column_if_missing(
        "reviews", sa.Column("ai_retryable_at", sa.DateTime(), nullable=True)
    )
    _add_column_if_missing(
        "reviews", sa.Column("ai_attempt_id", sa.String(length=36), nullable=True)
    )

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("evidence"):
        op.execute(
            sa.text(
                """
                UPDATE reviews
                SET ai_state = 'AI_COMPLETED'
                WHERE EXISTS (
                    SELECT 1 FROM evidence
                    WHERE evidence.review_id = reviews.id
                      AND evidence.evidence_type = 'ai_rationale'
                      AND evidence.source = 'gemini'
                )
                """
            )
        )
        op.execute(
            sa.text(
                """
                UPDATE pull_requests
                SET ai_state = 'AI_COMPLETED'
                WHERE EXISTS (
                    SELECT 1 FROM reviews
                    WHERE reviews.pull_request_id = pull_requests.id
                      AND reviews.head_sha = pull_requests.head_sha
                      AND reviews.ai_state = 'AI_COMPLETED'
                )
                """
            )
        )

    checks = {
        constraint["name"]: constraint
        for constraint in inspector.get_check_constraints("reviews")
        if constraint.get("name")
    }
    if "ck_reviews_decision" in checks:
        op.drop_constraint("ck_reviews_decision", "reviews", type_="check")
    op.create_check_constraint(
        "ck_reviews_decision",
        "reviews",
        "decision IN ('APPROVED', 'BLOCKED', 'MANUAL_REVIEW', "
        "'REVIEW_UNAVAILABLE', 'WAITING_FOR_AI', 'WAITING_FOR_CI', 'PENDING')",
    )


def downgrade() -> None:
    raise RuntimeError(
        "This AI lifecycle migration is forward-only. Removing persisted "
        "analysis states or decision history could misrepresent prior reviews."
    )
