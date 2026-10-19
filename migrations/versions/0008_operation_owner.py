"""add operations.owner

Revision ID: 0008_operation_owner
Revises: 0007_operations
Create Date: 2026-10-19

Adds the stable, non-secret caller identity used for scoped operation status
lookups and orphan recovery. The column is nullable so every existing row
stays valid; it is intentionally not part of the idempotency payload.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0008_operation_owner"
down_revision = "0007_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("operations")
    }
    if "owner" in columns:
        return
    op.add_column("operations", sa.Column("owner", sa.String(), nullable=True))


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("operations")
    }
    if "owner" not in columns:
        return
    with op.batch_alter_table("operations") as batch:
        batch.drop_column("owner")
