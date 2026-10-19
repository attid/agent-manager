"""Concurrency acceptance tests: WAL readers, serialized writers."""

from __future__ import annotations

import pathlib
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from agent_manager.storage import Base, Database, Task, TaskRepository


def test_threads_create_tasks_without_lost_rows(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    repository = TaskRepository(database)
    workers = 8
    per_worker = 15
    try:
        def create_batch(worker: int) -> None:
            for index in range(per_worker):
                repository.create(
                    title=f"worker-{worker}-{index}",
                    workspace="shared",
                )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(create_batch, range(workers)))

        rows = repository.list(workspace="shared")
        assert len(rows) == workers * per_worker  # no lost rows
        assert len({row.id for row in rows}) == workers * per_worker
    finally:
        database.dispose()


def test_reader_sees_committed_data_during_uncommitted_write(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        committed = repository.create(title="committed-before")

        writer = database.session_factory()
        try:
            writer.add(
                Task(
                    id="pending-row",
                    title="uncommitted",
                    description=None,
                    status="incoming",
                    workspace=None,
                    meta={},
                )
            )
            writer.flush()  # write lock held, transaction still open

            with database.session() as reader:
                visible_ids = {
                    task.id for task in reader.scalars(select(Task)).all()
                }
            assert committed.id in visible_ids
            assert "pending-row" not in visible_ids

            # A second connection also must not observe the uncommitted row.
            assert repository.get("pending-row") is None
        finally:
            writer.commit()
            writer.close()

        assert repository.get("pending-row") is not None
        assert {task.id for task in repository.list()} == {
            committed.id,
            "pending-row",
        }
    finally:
        database.dispose()


def test_base_metadata_matches_migrated_schema(
    migrated_db: pathlib.Path,
) -> None:
    """Model metadata and the migrated schema must not drift apart.

    The expectation grew from ``{"tasks"}`` to include the conversation and
    message tables added by revision 0002, the ``turns`` table added by
    revision 0003, the ``agent_profiles`` table added by revision 0004, the
    ``conversation_runtimes`` table added by revision 0005 and the durable
    ``operations`` table added by revision 0007; the migration chain is still
    the single source of the physical schema.
    """
    from sqlalchemy import inspect

    database = Database.from_path(migrated_db)
    try:
        expected = {
            "tasks",
            "conversations",
            "messages",
            "turns",
            "agent_profiles",
            "conversation_runtimes",
            "operations",
        }
        assert Base.metadata.tables.keys() == expected
        migrated = set(inspect(database.engine).get_table_names())
        assert migrated - {"alembic_version"} == expected
    finally:
        database.dispose()
