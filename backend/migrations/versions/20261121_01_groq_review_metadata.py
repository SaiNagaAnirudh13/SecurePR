"""Record AI metadata, fencing tokens, and merge-policy revisions.

Revision ID: 20261121_01
Revises: 20261120_04
"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_01"
down_revision = "20261120_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if (
        not inspector.has_table("reviews")
        or not inspector.has_table("background_jobs")
        or not inspector.has_table("auto_approval_eligibility")
    ):
        raise RuntimeError(
            "Cannot add provider metadata, job fencing, or policy revisions: "
            "required tables are missing."
        )
    existing_columns = {column["name"] for column in inspector.get_columns("reviews")}
    for name, column_type in (
        ("ai_provider", sa.String(length=32)),
        ("ai_model", sa.String(length=128)),
        ("ai_input_tokens", sa.Integer()),
        ("ai_output_tokens", sa.Integer()),
    ):
        if name not in existing_columns:
            op.add_column("reviews", sa.Column(name, column_type))
    job_columns = {
        column["name"] for column in inspector.get_columns("background_jobs")
    }
    if "lease_token" not in job_columns:
        op.add_column(
            "background_jobs",
            sa.Column(
                "lease_token",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    eligibility_columns = {
        column["name"] for column in inspector.get_columns("auto_approval_eligibility")
    }
    if "policy_revision" not in eligibility_columns:
        op.add_column(
            "auto_approval_eligibility",
            sa.Column(
                "policy_revision",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    if "auto_merge_policy_revision" not in existing_columns:
        op.add_column(
            "reviews",
            sa.Column("auto_merge_policy_revision", sa.BigInteger(), nullable=True),
        )
    if inspector.has_table("evidence"):
        op.execute(
            "UPDATE evidence SET source = 'ai_provider' "
            "WHERE source IN ('gemini', 'groq')"
        )


def downgrade() -> None:
    op.drop_column("reviews", "auto_merge_policy_revision")
    op.drop_column("auto_approval_eligibility", "policy_revision")
    op.drop_column("reviews", "ai_output_tokens")
    op.drop_column("reviews", "ai_input_tokens")
    op.drop_column("reviews", "ai_model")
    op.drop_column("reviews", "ai_provider")
    op.drop_column("background_jobs", "lease_token")
