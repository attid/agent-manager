"""Durable Telegram message links and the delivery outbox."""
from alembic import op
import sqlalchemy as sa

revision = "0012_telegram_delivery"
down_revision = "0011_task_subscriptions"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "telegram_message_links" not in existing:
        op.create_table(
            "telegram_message_links",
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("message_id", sa.String(), nullable=True),
            sa.Column("turn_id", sa.String(), nullable=True),
            sa.Column("task_id", sa.String(), nullable=True),
            sa.Column("origin", sa.String(), nullable=False, server_default="user"),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint(
                "chat_id", "telegram_message_id", name="pk_telegram_message_links"
            ),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversations.id"],
                name="fk_telegram_message_links_conversation",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["message_id"],
                ["messages.id"],
                name="fk_telegram_message_links_message",
                ondelete="SET NULL",
            ),
        )
        op.create_index(
            "ix_telegram_message_links_conversation",
            "telegram_message_links",
            ["conversation_id"],
        )
    if "telegram_deliveries" not in existing:
        op.create_table(
            "telegram_deliveries",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=True),
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("message_id", sa.String(), nullable=True),
            sa.Column("turn_id", sa.String(), nullable=True),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("parts_total", sa.Integer(), nullable=True),
            sa.Column("parts_sent", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "telegram_message_ids", sa.JSON(), nullable=False, server_default="[]"
            ),
            sa.Column(
                "attempt_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
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
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("id", name="uq_telegram_deliveries_id"),
            sa.UniqueConstraint(
                "idempotency_key", name="uq_telegram_deliveries_idempotency_key"
            ),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversations.id"],
                name="fk_telegram_deliveries_conversation",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["message_id"],
                ["messages.id"],
                name="fk_telegram_deliveries_message",
                ondelete="SET NULL",
            ),
        )
        op.create_index(
            "ix_telegram_deliveries_status", "telegram_deliveries", ["status"]
        )
        op.create_index(
            "ix_telegram_deliveries_conversation",
            "telegram_deliveries",
            ["conversation_id"],
        )


def downgrade():
    # Downgrading destroys the delivery history and Telegram message links;
    # conversations, messages and turns are not affected.
    op.drop_index(
        "ix_telegram_deliveries_conversation", table_name="telegram_deliveries"
    )
    op.drop_index("ix_telegram_deliveries_status", table_name="telegram_deliveries")
    op.drop_table("telegram_deliveries")
    op.drop_index(
        "ix_telegram_message_links_conversation", table_name="telegram_message_links"
    )
    op.drop_table("telegram_message_links")
