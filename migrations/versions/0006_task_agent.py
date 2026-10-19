"""Persist the selected task executor independently of its workspace."""

import sqlalchemy as sa
from alembic import op

revision = "0006_task_agent"
down_revision = "0005_create_conversation_runtimes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("agent_id", sa.String(), nullable=True))


def downgrade():
    op.drop_column("tasks", "agent_id")
