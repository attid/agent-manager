"""create turns table

Revision ID: 0003_create_turns
Revises: 0002_create_conversations_messages
Create Date: 2026-10-17

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003_create_turns"
down_revision = "0002_create_conversations_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "turns" in existing:
        return
    op.create_table(
        "turns",
        sa.Column(
            "sequence",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("input_message_id", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("claim_token", sa.String(), nullable=True),
        sa.Column("reply_message_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
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
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_turns_conversation_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["input_message_id"],
            ["messages.id"],
            name="fk_turns_input_message_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reply_message_id"],
            ["messages.id"],
            name="fk_turns_reply_message_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("id", name="uq_turns_id"),
        sa.UniqueConstraint(
            "input_message_id", name="uq_turns_input_message_id"
        ),
    )
    op.create_index(
        "ix_turns_conversation_status",
        "turns",
        ["conversation_id", "status"],
    )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "turns" in existing:
        op.drop_index("ix_turns_conversation_status", table_name="turns")
        op.drop_table("turns")
