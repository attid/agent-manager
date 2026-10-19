"""Migration, WAL and per-connection pragma acceptance tests."""

from __future__ import annotations

import pathlib
import sqlite3

from alembic import command
from sqlalchemy import inspect, text

from agent_manager.storage.database import Database
from agent_manager.storage.repository import TaskRepository

from .conftest import alembic_config, upgrade_to_head


def test_fresh_file_database_migrates_to_head(
    db_path: pathlib.Path,
) -> None:
    assert not db_path.exists()
    upgrade_to_head(db_path)
    assert db_path.exists()

    database = Database.from_path(db_path)
    try:
        inspector = inspect(database.engine)
        tables = set(inspector.get_table_names())
        assert {"tasks", "conversations", "messages", "turns"} <= tables
        assert "conversation_runtimes" in tables
        columns = {column["name"] for column in inspector.get_columns("tasks")}
        assert columns == {
            "id",
            "number",
            "title",
            "description",
            "status",
            "workspace",
            "agent_id",
            "created_at",
            "updated_at",
            "metadata",
        }
        operation_columns = {
            column["name"] for column in inspector.get_columns("operations")
        }
        assert "owner" in operation_columns
    finally:
        database.dispose()


def test_conversations_and_messages_schema(
    migrated_db: pathlib.Path,
) -> None:
    """The 0002 revision added the conversation/message tables."""
    database = Database.from_path(migrated_db)
    try:
        inspector = inspect(database.engine)
        conversation_columns = {
            column["name"]
            for column in inspector.get_columns("conversations")
        }
        assert conversation_columns == {
            "id",
            "title",
            "created_at",
            "updated_at",
            "metadata",
        }
        message_columns = {
            column["name"] for column in inspector.get_columns("messages")
        }
        assert message_columns == {
            "sequence",
            "id",
            "conversation_id",
            "role",
            "text",
            "source",
            "external_id",
            "created_at",
            "metadata",
        }
        foreign_keys = inspector.get_foreign_keys("messages")
        assert any(
            foreign_key["referred_table"] == "conversations"
            and foreign_key["constrained_columns"] == ["conversation_id"]
            for foreign_key in foreign_keys
        )
        unique_names = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("messages")
        }
        assert "uq_messages_conversation_source_external_id" in unique_names
        assert "uq_messages_id" in unique_names
    finally:
        database.dispose()


def test_upgrade_from_0001_preserves_existing_tasks(
    db_path: pathlib.Path,
) -> None:
    """Applying 0002 must not drop or rewrite rows created on 0001."""
    config = alembic_config(db_path)
    command.upgrade(config, "0001_create_tasks")

    database = Database.from_path(db_path)
    try:
        # Seed using the old schema, without referencing columns introduced later.
        with database.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO tasks (id, title, workspace) VALUES ('keep', 'keep me', 'proj-a')")
    finally:
        database.dispose()

    command.upgrade(config, "head")

    database = Database.from_path(db_path)
    try:
        fetched = TaskRepository(database).get("keep")
        assert fetched is not None
        assert fetched.title == "keep me"
        assert fetched.workspace == "proj-a"
        assert {task.id for task in TaskRepository(database).list()} == {"keep"}
        assert {"conversations", "messages"} <= set(
            inspect(database.engine).get_table_names()
        )
    finally:
        database.dispose()


def test_repeated_upgrade_is_safe(db_path: pathlib.Path) -> None:
    config = alembic_config(db_path)
    command.upgrade(config, "head")
    command.upgrade(config, "head")  # second run must be a no-op

    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
    finally:
        connection.close()
    # The single head moved from 0003 to 0004 when the agent_profiles table
    # was added, to 0005 when conversation_runtimes was added, to 0006 when the
    # task assignment column was added, to 0007 when the durable operations
    # table was added, to 0008 when the operation owner column was added, and
    # to 0009 when the analysis reports and recommendations tables were added;
    # the assertion that exactly one version row is stored, and that it is the
    # current head, is unchanged.
    assert rows == [("0011_task_subscriptions",)]


def test_wal_journal_mode_is_enabled(migrated_db: pathlib.Path) -> None:
    connection = sqlite3.connect(migrated_db)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert mode.lower() == "wal"
    # WAL creates sidecar files once a transaction has touched the database.
    assert migrated_db.exists()


def test_foreign_keys_and_busy_timeout_per_connection(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        for _ in range(2):  # every connection, not only the first
            with database.engine.connect() as connection:
                foreign_keys = connection.exec_driver_sql(
                    "PRAGMA foreign_keys"
                ).scalar()
                busy_timeout = connection.exec_driver_sql(
                    "PRAGMA busy_timeout"
                ).scalar()
            assert foreign_keys == 1
            assert busy_timeout == database.busy_timeout_ms
    finally:
        database.dispose()


def test_migrations_set_foreign_keys_pragma(
    migrated_db: pathlib.Path,
) -> None:
    connection = sqlite3.connect(migrated_db)
    try:
        # A SQLite connection opened outside SQLAlchemy defaults to OFF; this
        # documents that the app/migration boundary turns it on per connection.
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        connection.close()

    database = Database.from_path(migrated_db)
    try:
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        connection = database.engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys")
            assert cursor.fetchone()[0] == 1
            cursor.close()
        finally:
            connection.close()
    finally:
        database.dispose()


def test_alembic_version_records_single_head(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        with database.engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM alembic_version")
            ).scalar()
    finally:
        database.dispose()
    assert count == 1
