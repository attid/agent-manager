"""HTTP API acceptance tests against migrated temporary file databases."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from agent_manager.api import create_app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _app(db_path: pathlib.Path):
    """Build an application for a file database path."""
    return create_app(f"sqlite:///{db_path}")


def test_health(migrated_db: pathlib.Path) -> None:
    with TestClient(_app(migrated_db)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_task_returns_201_with_storage_fields(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        response = client.post(
            "/tasks",
            json={
                "title": "First task",
                "description": "do the thing",
                "workspace": "proj-a",
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["title"] == "First task"
    assert body["description"] == "do the thing"
    assert body["status"] == "incoming"
    assert body["workspace"] == "proj-a"
    assert body["created_at"]
    assert body["updated_at"]
    assert body["metadata"] == {}


def test_create_task_accepts_explicit_id(migrated_db: pathlib.Path) -> None:
    with TestClient(_app(migrated_db)) as client:
        response = client.post("/tasks", json={"id": "task-1", "title": "named"})
        fetched = client.get("/tasks/task-1")
    assert response.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json()["id"] == "task-1"


def test_create_task_duplicate_id_returns_409(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        first = client.post("/tasks", json={"id": "dup", "title": "one"})
        second = client.post("/tasks", json={"id": "dup", "title": "two"})
        fetched = client.get("/tasks/dup")
    assert first.status_code == 201
    assert second.status_code == 409
    assert "dup" in second.json()["detail"]
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "one"


def test_invalid_requests_are_rejected(migrated_db: pathlib.Path) -> None:
    with TestClient(_app(migrated_db)) as client:
        missing_title = client.post("/tasks", json={"description": "no title"})
        bad_metadata = client.post(
            "/tasks", json={"title": "x", "metadata": ["not", "a", "mapping"]}
        )
    assert missing_title.status_code == 422
    assert bad_metadata.status_code == 422


def test_missing_task_returns_404(migrated_db: pathlib.Path) -> None:
    with TestClient(_app(migrated_db)) as client:
        response = client.get("/tasks/does-not-exist")
    assert response.status_code == 404


def test_list_tasks(migrated_db: pathlib.Path) -> None:
    with TestClient(_app(migrated_db)) as client:
        client.post("/tasks", json={"title": "one", "workspace": "a"})
        client.post("/tasks", json={"title": "two", "workspace": "b"})
        listed = client.get("/tasks")
        filtered = client.get("/tasks", params={"workspace": "b"})
    assert listed.status_code == 200
    assert {task["title"] for task in listed.json()} == {"one", "two"}
    assert [task["title"] for task in filtered.json()] == ["two"]


def test_metadata_roundtrip_and_free_form_values(
    migrated_db: pathlib.Path,
) -> None:
    payload = {
        "tags": ["a", "b"],
        "priority": 3,
        "nested": {"deep": [1, 2, {"x": True}]},
    }
    long_title = "x" * 20_000
    with TestClient(_app(migrated_db)) as client:
        created = client.post(
            "/tasks",
            json={
                "title": long_title,
                "status": "a-status-not-in-any-list",
                "metadata": payload,
            },
        )
        fetched = client.get(f"/tasks/{created.json()['id']}")
    assert created.status_code == 201
    body = fetched.json()
    assert body["metadata"] == payload
    assert body["status"] == "a-status-not-in-any-list"
    assert body["title"] == long_title


def test_persistence_across_distinct_app_instances(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as first:
        created = first.post(
            "/tasks",
            json={"title": "durable", "metadata": {"k": "v"}},
        ).json()

    with TestClient(_app(migrated_db)) as second:
        response = second.get(f"/tasks/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "durable"
    assert body["metadata"] == {"k": "v"}


def test_import_does_not_create_database(tmp_path: pathlib.Path) -> None:
    db_file = tmp_path / "import-side-effect.db"
    env = os.environ.copy()
    env["AGENT_MANAGER_DB_URL"] = f"sqlite:///{db_file}"
    env["PYTHONPATH"] = "src"
    result = subprocess.run(
        [sys.executable, "-c", "import agent_manager.api.app"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not db_file.exists()


def test_startup_requires_migrated_database(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(RuntimeError):
        with TestClient(_app(missing)):
            pass
    assert not missing.exists()


def test_patch_task_updates_status_and_metadata(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        created = client.post(
            "/tasks",
            json={"title": "Draft task", "status": "incoming"},
        ).json()
        task_id = created["id"]

        patch_res = client.patch(
            f"/tasks/{task_id}",
            json={
                "status": "in_progress",
                "metadata": {"conversation_id": "conv-123"},
            },
        )
        assert patch_res.status_code == 200
        patched = patch_res.json()
        assert patched["status"] == "in_progress"
        assert patched["metadata"] == {"conversation_id": "conv-123"}
        assert patched["title"] == "Draft task"

        # Verify persistence
        fetched = client.get(f"/tasks/{task_id}").json()
        assert fetched["status"] == "in_progress"
        assert fetched["metadata"] == {"conversation_id": "conv-123"}


def test_patch_task_returns_404_for_unknown_task(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        res = client.patch(
            "/tasks/unknown-task-id",
            json={"status": "done"},
        )
        assert res.status_code == 404
        assert "task not found" in res.json()["detail"]


def test_create_turn_and_get_turn_flow(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        conv = client.post("/conversations", json={"title": "Test Conv"}).json()
        conv_id = conv["id"]

        msg = client.post(
            f"/conversations/{conv_id}/messages",
            json={"text": "Hello secretary", "role": "user"},
        ).json()
        msg_id = msg["id"]

        # Enqueue turn
        turn_res = client.post(
            f"/conversations/{conv_id}/turns",
            json={"input_message_id": msg_id, "metadata": {"source": "telegram"}},
        )
        assert turn_res.status_code == 201
        turn = turn_res.json()
        assert turn["conversation_id"] == conv_id
        assert turn["input_message_id"] == msg_id
        assert turn["status"] == "pending"
        assert turn["metadata"] == {"source": "telegram"}

        # Get turn by ID
        get_res = client.get(f"/turns/{turn['id']}")
        assert get_res.status_code == 200
        assert get_res.json()["id"] == turn["id"]
        assert get_res.json()["status"] == "pending"


def test_create_turn_error_handling(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        conv = client.post("/conversations", json={}).json()
        conv_id = conv["id"]

        # Missing message
        res1 = client.post(
            f"/conversations/{conv_id}/turns",
            json={"input_message_id": "nonexistent-msg"},
        )
        assert res1.status_code == 404

        # Non-user message
        assistant_msg = client.post(
            f"/conversations/{conv_id}/messages",
            json={"text": "I am bot", "role": "assistant"},
        ).json()
        res2 = client.post(
            f"/conversations/{conv_id}/turns",
            json={"input_message_id": assistant_msg["id"]},
        )
        assert res2.status_code == 400
        assert "expected 'user'" in res2.json()["detail"]

