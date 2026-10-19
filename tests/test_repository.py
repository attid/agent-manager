"""Repository behaviour tests against a migrated file database."""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy.exc import IntegrityError

from agent_manager.storage import (
    Database,
    DuplicateTaskIdError,
    TaskImportResult,
    TaskNotFoundError,
    TaskRepository,
)


def test_create_get_and_list(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        first = repository.create(
            title="First task",
            description="do the thing",
            workspace="proj-a",
        )
        second = repository.create(
            title="Second task",
            status="waiting",
            workspace="proj-b",
            metadata={"origin": "api", "nested": {"n": 1}},
        )

        fetched = repository.get(first.id)
        assert fetched is not None
        assert fetched.title == "First task"
        assert fetched.status == "incoming"
        assert fetched.workspace == "proj-a"
        assert fetched.created_at is not None
        assert fetched.updated_at is not None

        listed = repository.list()
        assert {task.id for task in listed} == {first.id, second.id}
        expected_order = [
            task.id
            for task in sorted(
                (first, second), key=lambda task: (task.created_at, task.id)
            )
        ]
        assert [t.id for t in listed] == expected_order

        assert repository.get("does-not-exist") is None
        assert [t.id for t in repository.list(status="waiting")] == [second.id]
        assert [t.id for t in repository.list(workspace="proj-a")] == [first.id]
        assert [t.id for t in repository.list(limit=1)] == [first.id]
        assert [t.id for t in repository.list(offset=1)] == [second.id]
    finally:
        database.dispose()


def test_metadata_json_round_trips(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        payload = {
            "tags": ["a", "b"],
            "priority": 3,
            "nested": {"deep": [1, 2, {"x": True}]},
        }
        created = repository.create(title="json", metadata=payload)
        fetched = repository.get(created.id)
        assert fetched is not None
        assert fetched.meta == payload
    finally:
        database.dispose()


def test_no_length_caps_or_status_allowlist(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        long_title = "x" * 20_000
        created = repository.create(
            title=long_title,
            description="y" * 20_000,
            status="a-status-not-in-any-list",
        )
        fetched = repository.get(created.id)
        assert fetched is not None
        assert fetched.title == long_title
        assert fetched.status == "a-status-not-in-any-list"
    finally:
        database.dispose()


def test_close_and_reopen_retains_records(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    repository = TaskRepository(database)
    created = repository.create(title="durable", workspace="proj")
    database.dispose()

    reopened = Database.from_path(migrated_db)
    try:
        fetched = TaskRepository(reopened).get(created.id)
        assert fetched is not None
        assert fetched.title == "durable"
        assert fetched.workspace == "proj"
    finally:
        reopened.dispose()


def test_explicit_id_is_respected(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        created = repository.create(id="task-1", title="named")
        assert created.id == "task-1"
        assert repository.get("task-1") is not None
    finally:
        database.dispose()


def test_duplicate_explicit_id_raises_focused_error(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        repository.create(id="task-1", title="one")
        with pytest.raises(DuplicateTaskIdError) as excinfo:
            repository.create(id="task-1", title="two")
        assert excinfo.value.task_id == "task-1"
        original = repository.get("task-1")
        assert original is not None
        assert original.title == "one"
    finally:
        database.dispose()


def test_unrelated_integrity_error_is_not_translated(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        with pytest.raises(IntegrityError):
            repository.create(id="not-null", title=None)  # type: ignore[arg-type]
    finally:
        database.dispose()


def test_import_tasks_is_idempotent(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        records = [
            {"id": "imp-1", "title": "one", "metadata": {"k": 1}},
            {"id": "imp-2", "title": "two"},
        ]
        first = repository.import_tasks(records)
        second = repository.import_tasks(records)
        assert first == TaskImportResult(created=2, skipped=0)
        assert second == TaskImportResult(created=0, skipped=2)
        assert len(repository.list()) == 2
        imported = repository.get("imp-1")
        assert imported is not None
        assert imported.meta == {"k": 1}
    finally:
        database.dispose()


def test_task_update_and_not_found(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        created = repository.create(title="Original", status="incoming", description="desc")
        assert created.title == "Original"
        assert created.status == "incoming"

        # Partial update: status and metadata
        updated = repository.update(
            created.id,
            status="in_progress",
            metadata={"assigned_agent": "secretary"},
        )
        assert updated.id == created.id
        assert updated.title == "Original"
        assert updated.status == "in_progress"
        assert updated.description == "desc"
        assert updated.meta == {"assigned_agent": "secretary"}

        # Clearing description with None
        updated2 = repository.update(created.id, description=None)
        assert updated2.description is None

        # Updating non-existent task raises TaskNotFoundError
        with pytest.raises(TaskNotFoundError) as exc:
            repository.update("missing-id", status="done")
        assert exc.value.task_id == "missing-id"
    finally:
        database.dispose()


def test_claim_in_progress_workspace_isolation(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    repository = TaskRepository(database)
    try:
        # Task 1 in workspace 'scannet' is waiting to commit
        t1 = repository.create(
            title="Scannet task 1",
            workspace="scannet",
            status="ready_to_commit",
        )
        # Task 2 in workspace 'scannet' is in_progress waiting for execution
        t2 = repository.create(
            title="Scannet task 2",
            workspace="scannet",
            status="in_progress",
        )
        # Task 3 in another workspace 'analytics' is in_progress
        t3 = repository.create(
            title="Analytics task 3",
            workspace="analytics",
            status="in_progress",
        )

        # Worker attempting to claim next task:
        # Task 2 must NOT be claimed because 'scannet' has t1 in ready_to_commit!
        # Task 3 MUST be claimed because 'analytics' is free.
        claimed = repository.claim_in_progress(worker_id="worker-1")
        assert claimed is not None
        assert claimed.id == t3.id
        assert claimed.workspace == "analytics"

        # Now no other task can be claimed
        claimed_none = repository.claim_in_progress(worker_id="worker-2")
        assert claimed_none is None

        # Once t1 moves to 'done' (committed), t2 becomes eligible
        repository.update(t1.id, status="done")
        claimed_t2 = repository.claim_in_progress(worker_id="worker-3")
        assert claimed_t2 is not None
        assert claimed_t2.id == t2.id
    finally:
        database.dispose()

