"""Add revision-scoped operation leases for concurrent review workers.

Revision ID: 20261121_02
Revises: 20261121_01
"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_02"
down_revision = "20261121_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("revision_operations"):
        op.create_table(
            "revision_operations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("repository_id", sa.String(length=36), nullable=False),
            sa.Column("pull_request_id", sa.String(length=36), nullable=False),
            sa.Column("head_sha", sa.String(length=64), nullable=False),
            sa.Column("operation", sa.String(length=128), nullable=False),
            sa.Column(
                "state",
                sa.String(length=16),
                server_default=sa.text("'QUEUED'"),
                nullable=False,
            ),
            sa.Column(
                "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
            ),
            sa.Column("owner_token", sa.String(length=36), nullable=True),
            sa.Column(
                "available_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("lease_until", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["pull_request_id"], ["pull_requests.id"]
            ),
            sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "repository_id",
                "pull_request_id",
                "head_sha",
                "operation",
                name="uq_revision_operation_identity",
            ),
        )
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("revision_operations")
    }
    if "ix_revision_operations_lease" not in indexes:
        op.create_index(
            "ix_revision_operations_lease",
            "revision_operations",
            ["state", "available_at", "lease_until"],
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("revision_operations"):
        op.drop_table("revision_operations")
