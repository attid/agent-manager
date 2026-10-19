"""create analysis reports and recommendations tables

Revision ID: 0009_analysis
Revises: 0008_operation_owner
Create Date: 2026-10-19

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0009_analysis"
down_revision = "0008_operation_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "analysis_reports" not in existing:
        op.create_table(
            "analysis_reports",
            sa.Column(
                "sequence", sa.Integer(), autoincrement=True, nullable=False
            ),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("agent_id", sa.String(), nullable=False),
            sa.Column("profile_revision", sa.Integer(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column(
                "report",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column(
                "evidence",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
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
            sa.PrimaryKeyConstraint("sequence"),
            sa.UniqueConstraint("id", name="uq_analysis_reports_id"),
        )
        op.create_index(
            "ix_analysis_reports_task", "analysis_reports", ["task_id"]
        )

    if "recommendations" not in existing:
        op.create_table(
            "recommendations",
            sa.Column(
                "sequence", sa.Integer(), autoincrement=True, nullable=False
            ),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("report_id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=True),
            sa.Column("target_kind", sa.String(), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column("target_revision", sa.Integer(), nullable=True),
            sa.Column("target_content_hash", sa.String(), nullable=True),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column(
                "evidence",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column("proposed_content", sa.JSON(), nullable=False),
            sa.Column("diff", sa.Text(), nullable=True),
            sa.Column(
                "status",
                sa.String(),
                nullable=False,
                server_default=sa.text("'proposed'"),
            ),
            sa.Column("applied_revision", sa.Integer(), nullable=True),
            sa.Column("applied_content_hash", sa.String(), nullable=True),
            sa.Column("apply_detail", sa.JSON(), nullable=True),
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
            sa.PrimaryKeyConstraint("sequence"),
            sa.UniqueConstraint("id", name="uq_recommendations_id"),
            sa.ForeignKeyConstraint(
                ["report_id"], ["analysis_reports.id"]
            ),
        )
        op.create_index(
            "ix_recommendations_report", "recommendations", ["report_id"]
        )
        op.create_index(
            "ix_recommendations_task", "recommendations", ["task_id"]
        )
        op.create_index(
            "ix_recommendations_status", "recommendations", ["status"]
        )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "recommendations" in existing:
        op.drop_index(
            "ix_recommendations_status", table_name="recommendations"
        )
        op.drop_index(
            "ix_recommendations_task", table_name="recommendations"
        )
        op.drop_index(
            "ix_recommendations_report", table_name="recommendations"
        )
        op.drop_table("recommendations")
    if "analysis_reports" in existing:
        op.drop_index(
            "ix_analysis_reports_task", table_name="analysis_reports"
        )
        op.drop_table("analysis_reports")
