"""create conversations and messages tables

Revision ID: 0002_create_conversations_messages
Revises: 0001_create_tasks
Create Date: 2026-10-17

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002_create_conversations_messages"
down_revision = "0001_create_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "conversations" not in existing:
        op.create_table(
            "conversations",
            sa.Column("id", sa.String(), primary_key=True, nullable=False),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "metadata",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
    if "messages" not in existing:
        op.create_table(
            "messages",
            sa.Column(
                "sequence",
                sa.Integer(),
                primary_key=True,
                autoincrement=True,
                nullable=False,
            ),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column(
                "source",
                sa.String(),
                nullable=False,
                server_default=sa.text("'api'"),
            ),
            sa.Column("external_id", sa.String(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "metadata",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversations.id"],
                name="fk_messages_conversation_id",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("id", name="uq_messages_id"),
            sa.UniqueConstraint(
                "conversation_id",
                "source",
                "external_id",
                name="uq_messages_conversation_source_external_id",
            ),
        )
        op.create_index(
            "ix_messages_conversation_id", "messages", ["conversation_id"]
        )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "messages" in existing:
        op.drop_index("ix_messages_conversation_id", table_name="messages")
        op.drop_table("messages")
    if "conversations" in existing:
        op.drop_table("conversations")
