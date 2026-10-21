"""Repository behaviour tests against a migrated file database."""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy.exc import IntegrityError

from kuzya.storage import (
    Database,
    DuplicateTaskIdError,
    TaskAlreadyRunningError,
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
        # Entering in_progress records the cleared execution marker explicitly;
        # the stale terminal marker must not survive a resume/re-run.
        assert updated.meta == {
            "assigned_agent": "secretary",
            "execution_status": None,
        }

        # Clearing description with None
        updated2 = repository.update(created.id, description=None)
        assert updated2.description is None

        # Updating non-existent task raises TaskNotFoundError
        with pytest.raises(TaskNotFoundError) as exc:
            repository.update("missing-id", status="done")
        assert exc.value.task_id == "missing-id"
    finally:
        database.dispose()


def test_update_merges_metadata_and_resumes_completed_task(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        created = repository.create(
            title="Resume me",
            description="Original scope",
            status="done",
            metadata={
                "origin": "telegram",
                "execution_status": "completed",
                "summary": "first pass",
                "nested": {"round": 1},
            },
        )

        merged = repository.update(
            created.id,
            metadata={"assigned_agent": "secretary", "nested": {"round": 2}},
        )
        # Shallow merge preserves unrelated keys and replaces a supplied key.
        assert merged.meta["origin"] == "telegram"
        assert merged.meta["summary"] == "first pass"
        assert merged.meta["assigned_agent"] == "secretary"
        assert merged.meta["nested"] == {"round": 2}
        assert merged.meta["execution_status"] == "completed"

        resumed = repository.update(
            created.id,
            status="in_progress",
            metadata={
                "execution_status": "completed",
                "additional_instructions": [
                    {"text": "Add a regression test.", "timestamp": "2025-01-02T03:04:05Z"}
                ],
            },
        )
        assert resumed.status == "in_progress"
        assert resumed.meta["execution_status"] is None
        assert resumed.meta["origin"] == "telegram"
        assert resumed.meta["summary"] == "first pass"
        assert resumed.meta["additional_instructions"][0]["text"] == (
            "Add a regression test."
        )

        # The stale completed marker must no longer keep the task out of claims.
        claimed = repository.claim_in_progress(worker_id="worker-resume")
        assert claimed is not None
        assert claimed.id == created.id
        assert claimed.meta["execution_status"] == "running"
    finally:
        database.dispose()


def test_update_in_progress_preserves_active_running_claim(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        created = repository.create(
            title="Already running",
            status="in_progress",
            workspace="running-workspace",
        )
        claimed = repository.claim_in_progress(worker_id="worker-active")
        assert claimed is not None
        assert claimed.id == created.id
        claimed_at = claimed.meta["execution_claimed_at"]

        # A stale card or retried button may patch in_progress again while the
        # first worker still owns the execution. The active marker must survive,
        # even when the payload tries to reintroduce a terminal status.
        updated = repository.update(
            created.id,
            status="in_progress",
            metadata={
                "execution_status": "completed",
                "execution_worker_id": "stale-worker",
                "additional_instructions": [
                    {"text": "New input exactly once.", "timestamp": "2025-01-02T03:04:05Z"}
                ],
            },
        )
        assert updated.status == "in_progress"
        assert updated.meta["execution_status"] == "running"
        assert updated.meta["execution_worker_id"] == "worker-active"
        assert updated.meta["execution_claimed_at"] == claimed_at
        assert updated.meta["additional_instructions"][0]["text"] == (
            "New input exactly once."
        )

        # The task cannot be claimed a second time while the first claim lives.
        assert repository.claim_in_progress(worker_id="worker-raced") is None
        reloaded = repository.get(created.id)
        assert reloaded is not None
        assert reloaded.meta["execution_status"] == "running"
        assert reloaded.meta["execution_worker_id"] == "worker-active"
    finally:
        database.dispose()


def test_update_in_progress_can_reject_active_running_claim(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        created = repository.create(title="Reject running", status="in_progress")
        claimed = repository.claim_in_progress(worker_id="worker-active")
        assert claimed is not None

        with pytest.raises(TaskAlreadyRunningError) as exc:
            repository.update(
                created.id,
                status="in_progress",
                metadata={"execution_status": None},
                reject_if_running=True,
            )
        assert exc.value.task_id == created.id
        assert exc.value.worker_id == "worker-active"
        assert "already executing" in str(exc.value)

        # Rejected updates leave both the claim marker and payload untouched.
        reloaded = repository.get(created.id)
        assert reloaded is not None
        assert reloaded.meta == claimed.meta
        assert repository.claim_in_progress(worker_id="worker-raced") is None
    finally:
        database.dispose()


def test_create_in_progress_clears_terminal_execution_status(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = TaskRepository(database)
        created = repository.create(
            title="New execution",
            status="in_progress",
            metadata={"execution_status": "completed", "origin": "api"},
        )
        assert created.meta["execution_status"] is None
        assert created.meta["origin"] == "api"
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

