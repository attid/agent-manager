"""HTTP acceptance tests for the conversation/message API.

These tests complement the storage-level ``tests/test_conversations.py`` by
exercising the same durability guarantees through a real ``TestClient``
against migrated temporary file databases. There is no graph entry for this
module yet; it sits on the existing ``agent_manager.api`` boundary only.
"""

from __future__ import annotations

import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from fastapi.testclient import TestClient

from agent_manager.api import create_app

from .conftest import alembic_config


def _app(db_path: pathlib.Path):
    """Build an application for a file database path."""
    return create_app(f"sqlite:///{db_path}")


def test_create_conversation_roundtrip_with_metadata(
    migrated_db: pathlib.Path,
) -> None:
    payload = {"tags": ["a", "b"], "nested": {"deep": [1, 2, {"x": True}]}}
    with TestClient(_app(migrated_db)) as client:
        created = client.post(
            "/conversations",
            json={"title": "Support", "metadata": payload},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["id"]
        assert body["title"] == "Support"
        assert body["metadata"] == payload
        assert body["created_at"]
        assert body["updated_at"]

        fetched = client.get(f"/conversations/{body['id']}")
        listed = client.get("/conversations")

    assert fetched.status_code == 200
    assert fetched.json() == body
    assert [conversation["id"] for conversation in listed.json()] == [
        body["id"]
    ]


def test_create_conversation_duplicate_id_returns_409(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        first = client.post(
            "/conversations", json={"id": "c-1", "title": "one"}
        )
        second = client.post(
            "/conversations", json={"id": "c-1", "title": "two"}
        )
        fetched = client.get("/conversations/c-1")
    assert first.status_code == 201
    assert second.status_code == 409
    assert "c-1" in second.json()["detail"]
    assert fetched.json()["title"] == "one"


def test_missing_conversation_returns_404(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        response = client.get("/conversations/absent")
    assert response.status_code == 404


def test_message_roundtrip_defaults_and_metadata(
    migrated_db: pathlib.Path,
) -> None:
    payload = {"provider": "api", "nested": {"n": 1}}
    with TestClient(_app(migrated_db)) as client:
        conversation = client.post("/conversations", json={}).json()
        url = f"/conversations/{conversation['id']}/messages"
        created = client.post(
            url, json={"text": "hello", "metadata": payload}
        )
        listed = client.get(url)

    assert created.status_code == 201
    body = created.json()
    assert body["replayed"] is False
    assert body["role"] == "user"
    assert body["source"] == "api"
    assert body["text"] == "hello"
    assert body["metadata"] == payload
    assert body["conversation_id"] == conversation["id"]
    assert body["external_id"] is None
    assert body["sequence"] > 0

    assert listed.status_code == 200
    messages = listed.json()
    assert len(messages) == 1
    # The list representation is the plain message, without replay metadata.
    assert "replayed" not in messages[0]
    assert messages[0]["id"] == body["id"]


def test_empty_conversation_lists_empty_but_absent_is_404(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        conversation = client.post("/conversations", json={}).json()
        empty = client.get(f"/conversations/{conversation['id']}/messages")
        absent = client.get("/conversations/absent/messages")
    assert empty.status_code == 200
    assert empty.json() == []
    assert absent.status_code == 404


def test_exact_retry_returns_200_and_conflict_409_without_overwrite(
    migrated_db: pathlib.Path,
) -> None:
    payload = {
        "role": "user",
        "text": "hello",
        "source": "telegram",
        "external_id": "update-1",
        "metadata": {"update_id": 1},
    }
    with TestClient(_app(migrated_db)) as client:
        conversation = client.post("/conversations", json={}).json()
        url = f"/conversations/{conversation['id']}/messages"
        first = client.post(url, json=payload)
        replay = client.post(url, json=payload)
        conflict = client.post(url, json={**payload, "text": "different"})
        messages = client.get(url).json()

    assert first.status_code == 201
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["sequence"] == first.json()["sequence"]

    assert conflict.status_code == 409
    assert len(messages) == 1
    assert messages[0]["text"] == "hello"
    assert messages[0]["role"] == "user"
    assert messages[0]["metadata"] == {"update_id": 1}


def test_append_to_absent_conversation_returns_404(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        response = client.post(
            "/conversations/absent/messages",
            json={"text": "hi", "external_id": "e"},
        )
    assert response.status_code == 404


def test_idempotency_key_is_scoped_by_source_and_conversation(
    migrated_db: pathlib.Path,
) -> None:
    payload = {"text": "hi", "external_id": "same"}
    with TestClient(_app(migrated_db)) as client:
        first = client.post("/conversations", json={}).json()
        second = client.post("/conversations", json={}).json()
        api = client.post(
            f"/conversations/{first['id']}/messages",
            json={**payload, "source": "api"},
        )
        telegram = client.post(
            f"/conversations/{first['id']}/messages",
            json={**payload, "source": "telegram"},
        )
        other_conversation = client.post(
            f"/conversations/{second['id']}/messages",
            json={**payload, "source": "api"},
        )
        first_messages = client.get(
            f"/conversations/{first['id']}/messages"
        ).json()
        second_messages = client.get(
            f"/conversations/{second['id']}/messages"
        ).json()

    assert api.status_code == 201
    assert telegram.status_code == 201
    assert other_conversation.status_code == 201
    assert len({api.json()["id"], telegram.json()["id"]}) == 2
    assert len(first_messages) == 2
    assert len(second_messages) == 1
    assert other_conversation.json()["id"] == second_messages[0]["id"]


def test_two_app_instances_against_same_database_see_same_conversation(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as first:
        created = first.post(
            "/conversations", json={"title": "durable"}
        ).json()
        first.post(
            f"/conversations/{created['id']}/messages",
            json={"text": "hello", "external_id": "e1"},
        )

    with TestClient(_app(migrated_db)) as second:
        fetched = second.get(f"/conversations/{created['id']}")
        messages = second.get(
            f"/conversations/{created['id']}/messages"
        )

    assert fetched.status_code == 200
    assert fetched.json()["title"] == "durable"
    assert [message["text"] for message in messages.json()] == ["hello"]


def test_existing_task_endpoints_are_unchanged(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        created = client.post(
            "/tasks", json={"title": "still here", "metadata": {"k": "v"}}
        )
        fetched = client.get(f"/tasks/{created.json()['id']}")
        listed = client.get("/tasks")
    assert created.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json()["metadata"] == {"k": "v"}
    assert [task["title"] for task in listed.json()] == ["still here"]


def test_startup_rejects_database_only_migrated_to_0001(
    db_path: pathlib.Path,
) -> None:
    """An 0001 database lacks the conversation tables and must fail early."""
    command.upgrade(alembic_config(db_path), "0001_create_tasks")

    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(_app(db_path)):
            pass

    message = str(excinfo.value)
    assert "conversations" in message
    assert "migration" in message.lower()

    # The application did not create schema while failing to start.
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "tasks" in tables
    assert "conversations" not in tables
    assert "messages" not in tables
