"""Assign stable human-readable task numbers without changing internal IDs."""

from alembic import op
import sqlalchemy as sa

revision = "0010_task_numbers"
down_revision = "0009_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("number", sa.Integer(), nullable=True))
    op.execute(sa.text("""
        WITH numbered AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, id) AS number
            FROM tasks
        )
        UPDATE tasks SET number = (
            SELECT numbered.number FROM numbered WHERE numbered.id = tasks.id
        )
    """))
    op.create_index("ix_tasks_number", "tasks", ["number"], unique=True)
    # Legacy SQL inserts also receive a number. ORM inserts allocate it in
    # their INSERT statement, so concurrent writers cannot select the same one.
    op.execute(sa.text("""
        CREATE TRIGGER tasks_assign_number AFTER INSERT ON tasks
        WHEN NEW.number IS NULL
        BEGIN
            UPDATE tasks SET number = (
                SELECT COALESCE(MAX(number), 0) + 1 FROM tasks
            ) WHERE id = NEW.id;
        END
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS tasks_assign_number"))
    op.drop_index("ix_tasks_number", table_name="tasks")
    op.drop_column("tasks", "number")
