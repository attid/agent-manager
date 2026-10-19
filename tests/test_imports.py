"""Idempotent task-history import acceptance tests.

Every test runs against a real migrated temporary file database and the real
HTTP backend, never an in-memory database.
"""

from __future__ import annotations

import pathlib
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from agent_manager.api import create_app


def _app(db_path: pathlib.Path):
    """Build an application for a file database path."""
    return create_app(f"sqlite:///{db_path}")


def test_initial_import_creates_records(migrated_db: pathlib.Path) -> None:
    payload = {
        "tasks": [
            {
                "id": "history-1",
                "title": "first",
                "description": "do it",
                "status": "done",
                "workspace": "proj-a",
                "metadata": {"role": "worker"},
            },
            {"id": "history-2", "title": "second"},
        ]
    }
    with TestClient(_app(migrated_db)) as client:
        response = client.post("/imports/tasks", json=payload)
        fetched = client.get("/tasks/history-1")
        listed = client.get("/tasks")
    assert response.status_code == 200
    assert response.json() == {"created": 2, "skipped": 0}
    body = fetched.json()
    assert body["title"] == "first"
    assert body["description"] == "do it"
    assert body["status"] == "done"
    assert body["workspace"] == "proj-a"
    assert body["metadata"] == {"role": "worker"}
    assert len(listed.json()) == 2


def test_exact_replay_is_idempotent(migrated_db: pathlib.Path) -> None:
    payload = {
        "tasks": [
            {"id": "history-1", "title": "first"},
            {"id": "history-2", "title": "second"},
        ]
    }
    with TestClient(_app(migrated_db)) as client:
        first = client.post("/imports/tasks", json=payload)
        replay = client.post("/imports/tasks", json=payload)
        listed = client.get("/tasks")
    assert first.json() == {"created": 2, "skipped": 0}
    assert replay.json() == {"created": 0, "skipped": 2}
    assert len(listed.json()) == 2


def test_changed_payload_with_existing_id_leaves_original(
    migrated_db: pathlib.Path,
) -> None:
    original = {
        "id": "history-1",
        "title": "original",
        "status": "incoming",
        "metadata": {"version": 1},
    }
    changed = {
        "id": "history-1",
        "title": "changed",
        "status": "failed",
        "metadata": {"version": 2},
    }
    with TestClient(_app(migrated_db)) as client:
        client.post("/imports/tasks", json={"tasks": [original]})
        replay = client.post("/imports/tasks", json={"tasks": [changed]})
        fetched = client.get("/tasks/history-1")
    assert replay.json() == {"created": 0, "skipped": 1}
    body = fetched.json()
    assert body["title"] == "original"
    assert body["status"] == "incoming"
    assert body["metadata"] == {"version": 1}


def test_duplicate_ids_within_batch_create_once(
    migrated_db: pathlib.Path,
) -> None:
    payload = {
        "tasks": [
            {"id": "duplicate", "title": "first"},
            {"id": "duplicate", "title": "second"},
            {"id": "duplicate", "title": "third"},
        ]
    }
    with TestClient(_app(migrated_db)) as client:
        response = client.post("/imports/tasks", json=payload)
        listed = client.get("/tasks")
    assert response.json() == {"created": 1, "skipped": 2}
    assert len(listed.json()) == 1
    assert listed.json()[0]["id"] == "duplicate"
    assert listed.json()[0]["title"] == "first"


def test_import_does_not_overwrite_existing_open_task(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        created = client.post(
            "/tasks",
            json={"id": "open-1", "title": "open", "status": "incoming"},
        )
        assert created.status_code == 201
        response = client.post(
            "/imports/tasks",
            json={
                "tasks": [
                    {"id": "open-1", "title": "imported", "status": "done"}
                ]
            },
        )
        fetched = client.get("/tasks/open-1")
    assert response.json() == {"created": 0, "skipped": 1}
    body = fetched.json()
    assert body["title"] == "open"
    assert body["status"] == "incoming"


def test_concurrent_imports_are_idempotent(migrated_db: pathlib.Path) -> None:
    payload = {
        "tasks": [{"id": f"conc-{i}", "title": f"task {i}"} for i in range(6)]
    }

    def post_batch() -> dict[str, int]:
        with TestClient(_app(migrated_db)) as client:
            response = client.post("/imports/tasks", json=payload)
            assert response.status_code == 200, response.text
            return response.json()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: post_batch(), range(3)))

    assert sum(result["created"] for result in results) == 6
    assert sum(result["created"] + result["skipped"] for result in results) == 18

    with TestClient(_app(migrated_db)) as client:
        listed = client.get("/tasks")
    ids = {task["id"] for task in listed.json()}
    assert ids == {f"conc-{i}" for i in range(6)}
    assert len(listed.json()) == 6


def test_invalid_item_causes_no_partial_writes(
    migrated_db: pathlib.Path,
) -> None:
    payload = {
        "tasks": [
            {"id": "valid", "title": "valid"},
            {"id": "invalid"},  # missing required title
        ]
    }
    with TestClient(_app(migrated_db)) as client:
        response = client.post("/imports/tasks", json=payload)
        listed = client.get("/tasks")
    assert response.status_code == 422
    assert listed.json() == []


def test_restart_persists_imported_records(migrated_db: pathlib.Path) -> None:
    payload = {
        "tasks": [
            {
                "id": "persisted-1",
                "title": "persisted",
                "metadata": {"origin": "history"},
            }
        ]
    }
    with TestClient(_app(migrated_db)) as first:
        created = first.post("/imports/tasks", json=payload)
    assert created.json() == {"created": 1, "skipped": 0}

    with TestClient(_app(migrated_db)) as second:
        fetched = second.get("/tasks/persisted-1")
        replay = second.post("/imports/tasks", json=payload)
    assert fetched.status_code == 200
    assert fetched.json()["metadata"] == {"origin": "history"}
    assert replay.json() == {"created": 0, "skipped": 1}


def test_execution_provenance_is_preserved(migrated_db: pathlib.Path) -> None:
    provenance = {
        "role": "worker",
        "provider": "anthropic",
        "model": "claude",
        "runner_session": "session-42",
        "artifacts": ["s3://bucket/a", "s3://bucket/b"],
        "validation": {"passed": True, "report": "all checks green"},
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "started_at": "2024-01-01T00:00:00Z",
        "completed_at": "2024-01-01T00:05:00Z",
    }
    payload = {
        "tasks": [
            {"id": "prov-meta", "title": "metadata", "metadata": provenance},
            {"id": "prov-extras", "title": "extras", **provenance},
        ]
    }
    with TestClient(_app(migrated_db)) as client:
        response = client.post("/imports/tasks", json=payload)
        from_metadata = client.get("/tasks/prov-meta").json()
        from_extras = client.get("/tasks/prov-extras").json()
    assert response.json() == {"created": 2, "skipped": 0}
    assert from_metadata["metadata"] == provenance
    for key, value in provenance.items():
        assert from_extras["metadata"][key] == value


def test_created_at_is_import_time_and_original_times_stay_in_metadata(
    migrated_db: pathlib.Path,
) -> None:
    original_started = "2020-05-05T12:00:00Z"
    payload = {
        "tasks": [
            {
                "id": "timestamps",
                "title": "timestamps",
                "metadata": {"started_at": original_started},
            }
        ]
    }
    with TestClient(_app(migrated_db)) as client:
        client.post("/imports/tasks", json=payload)
        body = client.get("/tasks/timestamps").json()
    assert body["created_at"]
    assert body["created_at"].startswith("20")
    assert body["metadata"]["started_at"] == original_started


def test_import_accepts_free_form_status_without_caps(
    migrated_db: pathlib.Path,
) -> None:
    payload = {
        "tasks": [
            {
                "id": "long",
                "title": "x" * 20_000,
                "status": "another-status-not-in-any-list",
            }
        ]
    }
    with TestClient(_app(migrated_db)) as client:
        response = client.post("/imports/tasks", json=payload)
        body = client.get("/tasks/long").json()
    assert response.json() == {"created": 1, "skipped": 0}
    assert body["status"] == "another-status-not-in-any-list"
    assert body["title"] == "x" * 20_000
