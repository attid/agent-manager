"""create conversation_runtimes table

Revision ID: 0005_create_conversation_runtimes
Revises: 0004_create_agent_profiles
Create Date: 2026-10-17

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0005_create_conversation_runtimes"
down_revision = "0004_create_agent_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "conversation_runtimes" in existing:
        return
    op.create_table(
        "conversation_runtimes",
        sa.Column("conversation_id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("profile_revision", sa.Integer(), nullable=False),
        sa.Column("role_export_path", sa.String(), nullable=True),
        sa.Column("snapshot_export_path", sa.String(), nullable=True),
        sa.Column("prompt_export_path", sa.String(), nullable=True),
        sa.Column("runner_state_dir", sa.String(), nullable=True),
        sa.Column("runner_session_id", sa.String(), nullable=True),
        sa.Column("reservation_token", sa.String(), nullable=True),
        sa.Column("reservation_turn_id", sa.String(), nullable=True),
        sa.Column("reservation_attempt_count", sa.Integer(), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("last_turn_id", sa.String(), nullable=True),
        sa.Column("last_reply_text", sa.Text(), nullable=True),
        sa.Column("last_provenance", sa.JSON(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_runtimes_conversation_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id", "profile_revision"],
            ["agent_profiles.agent_id", "agent_profiles.revision"],
            name="fk_conversation_runtimes_profile",
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "conversation_runtimes" in existing:
        op.drop_table("conversation_runtimes")
