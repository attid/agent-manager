"""Durable task completion subscriptions and reply delivery progress."""
from alembic import op
import sqlalchemy as sa

revision = "0011_task_subscriptions"
down_revision = "0010_task_numbers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_subscriptions",
        sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("conversation_id", sa.String(), sa.ForeignKey("conversations.id"), primary_key=True),
        sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("turn_id", sa.String(), nullable=True),
        sa.Column("parts_sent", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_table("task_subscriptions")
