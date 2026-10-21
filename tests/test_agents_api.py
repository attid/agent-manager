"""HTTP acceptance tests for the versioned agent-profile API.

These tests exercise the ``/agents`` endpoints through a real ``TestClient``
against migrated temporary file databases, complementing the storage-level
coverage in ``tests/test_agents.py``. There is no graph entry for this new
module yet; it sits on the existing ``kuzya.api`` boundary and wraps
``AgentProfileRepository`` without changing it.
"""

from __future__ import annotations

import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from fastapi.testclient import TestClient

from kuzya.api import create_app

from .conftest import alembic_config


def _app(db_path: pathlib.Path):
    """Build an application for a file database path."""
    return create_app(f"sqlite:///{db_path}")


def _create(client: TestClient, **overrides) -> dict:
    payload = {
        "agent_id": "agent-1",
        "name": "First",
        "role": "worker",
        "system_prompt": "You are helpful.",
    }
    payload.update(overrides)
    response = client.post("/agents", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_returns_revision_one_and_roundtrips(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        created = _create(
            client,
            runner_config={"command": "pi"},
            skills=[{"skill": "search"}],
            knowledge={"topic": "math"},
        )
        fetched = client.get("/agents/agent-1")

    assert created["agent_id"] == "agent-1"
    assert created["revision"] == 1
    assert created["name"] == "First"
    assert created["role"] == "worker"
    assert created["system_prompt"] == "You are helpful."
    assert created["runner_config"] == {"command": "pi"}
    assert created["skills"] == [{"skill": "search"}]
    assert created["knowledge"] == {"topic": "math"}
    assert created["created_at"]

    assert fetched.status_code == 200
    assert fetched.json() == created


def test_list_agents_returns_current_snapshot_per_agent(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        _create(client, agent_id="b", name="B1")
        _create(client, agent_id="a", name="A1")
        renamed = client.patch(
            "/agents/a", json={"expected_revision": 1, "name": "A2"}
        )
        listed = client.get("/agents")

    assert renamed.status_code == 200
    assert listed.status_code == 200
    assert [(item["agent_id"], item["revision"]) for item in listed.json()] == [
        ("a", 2),
        ("b", 1),
    ]
    assert [item["name"] for item in listed.json()] == ["A2", "B1"]


def test_profile_prompt_and_binding_roundtrip(
    migrated_db: pathlib.Path,
) -> None:
    runner_config = {
        "command": "pi",
        "args": ["--json", {"nested": [1, 2, 3]}],
        "flag": True,
    }
    skills = [
        {"skill": "search", "config": {"depth": 2}},
        {"skill": "write", "enabled": False},
        {"skill": "\u0440\u0443\u0441\u0441\u043a\u0438\u0439"},
    ]
    knowledge = {"notes": ["\u0441\u0442\u0440\u043e\u043a\u0430", "emoji \U0001f31f"]}
    prompt = "\u041f\u0440\u0438\u0432\u0435\u0442 \U0001f31f"
    with TestClient(_app(migrated_db)) as client:
        created = _create(
            client,
            role="a role not in any list",
            system_prompt=prompt,
            runner_config=runner_config,
            skills=skills,
            knowledge=knowledge,
        )
        fetched = client.get("/agents/agent-1")

    assert created["role"] == "a role not in any list"
    assert created["system_prompt"] == prompt
    assert created["runner_config"] == runner_config
    assert created["skills"] == skills
    assert created["knowledge"] == knowledge
    assert fetched.json() == created


def test_history_and_specific_revision(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        _create(client, name="Original", system_prompt="original prompt")
        updated = client.patch(
            "/agents/agent-1",
            json={"expected_revision": 1, "name": "Second"},
        )
        history = client.get("/agents/agent-1/revisions")
        first = client.get("/agents/agent-1/revisions/1")
        second = client.get("/agents/agent-1/revisions/2")
        missing_revision = client.get("/agents/agent-1/revisions/99")
        unknown_history = client.get("/agents/absent/revisions")

    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["name"] == "Second"

    assert history.status_code == 200
    assert [item["revision"] for item in history.json()] == [1, 2]
    assert history.json()[0]["name"] == "Original"
    assert history.json()[1]["name"] == "Second"

    assert first.status_code == 200
    assert first.json()["revision"] == 1
    assert first.json()["name"] == "Original"
    assert first.json()["system_prompt"] == "original prompt"
    assert second.status_code == 200
    assert second.json()["revision"] == 2

    assert missing_revision.status_code == 404
    # The repository lists an unknown agent's history as empty.
    assert unknown_history.status_code == 200
    assert unknown_history.json() == []


def test_patch_preserves_omitted_and_replaces_supplied_fields(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        _create(
            client,
            name="Original",
            role="worker",
            system_prompt="original prompt",
            runner_config={"seed": 1, "nested": {"a": 1, "b": 2}},
            skills=[{"skill": "one"}],
            knowledge={"k": "v"},
        )
        renamed = client.patch(
            "/agents/agent-1",
            json={"expected_revision": 1, "name": "Renamed"},
        )
        replaced = client.patch(
            "/agents/agent-1",
            json={
                "expected_revision": 2,
                "runner_config": {"nested": {"a": 3}},
                "skills": [],
            },
        )

    assert renamed.status_code == 200
    body = renamed.json()
    assert body["name"] == "Renamed"
    assert body["role"] == "worker"
    assert body["system_prompt"] == "original prompt"
    assert body["runner_config"] == {"seed": 1, "nested": {"a": 1, "b": 2}}
    assert body["skills"] == [{"skill": "one"}]
    assert body["knowledge"] == {"k": "v"}

    assert replaced.status_code == 200
    body = replaced.json()
    assert body["name"] == "Renamed"  # omitted, carried forward
    # Supplied JSON replaces the field whole, never a deep merge.
    assert body["runner_config"] == {"nested": {"a": 3}}
    assert body["skills"] == []
    assert body["knowledge"] == {"k": "v"}


def test_patch_stale_revision_conflicts_and_leaves_state_unchanged(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        _create(client, name="Original")
        client.patch(
            "/agents/agent-1", json={"expected_revision": 1, "name": "Second"}
        )
        stale = client.patch(
            "/agents/agent-1", json={"expected_revision": 1, "name": "Stale"}
        )
        current = client.get("/agents/agent-1")
        history = client.get("/agents/agent-1/revisions")

    assert stale.status_code == 409
    assert "current revision 2" in stale.json()["detail"]
    assert current.json()["revision"] == 2
    assert current.json()["name"] == "Second"
    assert [item["revision"] for item in history.json()] == [1, 2]
    assert [item["name"] for item in history.json()] == [
        "Original",
        "Second",
    ]


def test_create_duplicate_returns_409_with_current_revision(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        _create(client, agent_id="dup", name="Original")
        duplicate = client.post(
            "/agents",
            json={
                "agent_id": "dup",
                "name": "Second",
                "role": "worker",
                "system_prompt": "second",
            },
        )
        fetched = client.get("/agents/dup")

    assert duplicate.status_code == 409
    assert "dup" in duplicate.json()["detail"]
    assert "current revision 1" in duplicate.json()["detail"]
    assert fetched.json()["name"] == "Original"


def test_missing_agent_returns_404(migrated_db: pathlib.Path) -> None:
    with TestClient(_app(migrated_db)) as client:
        current = client.get("/agents/absent")
        revision = client.get("/agents/absent/revisions/1")
        patched = client.patch(
            "/agents/absent", json={"expected_revision": 1, "name": "Nope"}
        )

    assert current.status_code == 404
    assert revision.status_code == 404
    assert patched.status_code == 404


def test_patch_requires_expected_revision_and_rejects_identity_mutation(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        _create(client)
        missing_expected = client.patch("/agents/agent-1", json={"name": "X"})
        identity = client.patch(
            "/agents/agent-1",
            json={"expected_revision": 1, "agent_id": "other", "name": "X"},
        )
        revision = client.patch(
            "/agents/agent-1",
            json={"expected_revision": 1, "revision": 9, "name": "X"},
        )
        null_name = client.patch(
            "/agents/agent-1",
            json={"expected_revision": 1, "name": None},
        )
        null_skills = client.patch(
            "/agents/agent-1",
            json={"expected_revision": 1, "skills": None},
        )
        current = client.get("/agents/agent-1")

    assert missing_expected.status_code == 422
    assert identity.status_code == 422
    assert revision.status_code == 422
    assert null_name.status_code == 422
    assert null_skills.status_code == 422
    assert current.json()["revision"] == 1
    assert current.json()["name"] == "First"


def test_patch_expected_revision_only_appends_unchanged_revision(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        _create(
            client,
            role="worker",
            system_prompt="original prompt",
            runner_config={"seed": 1},
            skills=[{"skill": "one"}],
            knowledge={"k": "v"},
        )
        appended = client.patch("/agents/agent-1", json={"expected_revision": 1})
        stale = client.patch("/agents/agent-1", json={"expected_revision": 1})
        history = client.get("/agents/agent-1/revisions")

    assert appended.status_code == 200
    assert appended.json()["revision"] == 2
    assert appended.json()["name"] == "First"
    assert appended.json()["role"] == "worker"
    assert appended.json()["system_prompt"] == "original prompt"
    assert appended.json()["runner_config"] == {"seed": 1}
    assert appended.json()["skills"] == [{"skill": "one"}]
    assert appended.json()["knowledge"] == {"k": "v"}

    # Revision checks still apply to an unchanged snapshot.
    assert stale.status_code == 409
    assert "current revision 2" in stale.json()["detail"]
    assert [item["revision"] for item in history.json()] == [1, 2]


def test_skills_are_json_objects_with_free_keys_and_no_count_limit(
    migrated_db: pathlib.Path,
) -> None:
    bindings = [
        {"skill": f"s-{index}", "arbitrary": {"deep": [index]}, "flag": index % 2 == 0}
        for index in range(50)
    ]
    with TestClient(_app(migrated_db)) as client:
        created = _create(client, skills=bindings)
        scalar = client.post(
            "/agents",
            json={
                "agent_id": "scalar",
                "name": "Scalar",
                "role": "worker",
                "system_prompt": "x",
                "skills": ["not-an-object"],
            },
        )
        nested = client.post(
            "/agents",
            json={
                "agent_id": "nested",
                "name": "Nested",
                "role": "worker",
                "system_prompt": "x",
                "skills": [["not", "an", "object"]],
            },
        )
        bad_patch = client.patch(
            "/agents/agent-1",
            json={"expected_revision": 1, "skills": ["not-an-object"]},
        )
        current = client.get("/agents/agent-1")

    # The storage shape is a JSON array of binding objects; a scalar member
    # must be rejected by validation instead of reaching dict(binding).
    assert scalar.status_code == 422
    assert nested.status_code == 422
    assert bad_patch.status_code == 422
    assert created["skills"] == bindings
    assert current.json()["skills"] == bindings
    assert current.json()["revision"] == 1


def test_revisions_survive_restart(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as first:
        _create(client=first, name="Original")
        first.patch(
            "/agents/agent-1", json={"expected_revision": 1, "name": "Second"}
        )

    with TestClient(_app(migrated_db)) as second:
        current = second.get("/agents/agent-1")
        original = second.get("/agents/agent-1/revisions/1")
        history = second.get("/agents/agent-1/revisions")

    assert current.status_code == 200
    assert current.json()["revision"] == 2
    assert current.json()["name"] == "Second"
    assert original.status_code == 200
    assert original.json()["revision"] == 1
    assert original.json()["name"] == "Original"
    assert [item["revision"] for item in history.json()] == [1, 2]


def test_existing_task_and_conversation_endpoints_unchanged(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        task = client.post("/tasks", json={"title": "still here"})
        fetched_task = client.get(f"/tasks/{task.json()['id']}")
        conversation = client.post("/conversations", json={"title": "C"})
        message = client.post(
            f"/conversations/{conversation.json()['id']}/messages",
            json={"text": "hello"},
        )
        messages = client.get(
            f"/conversations/{conversation.json()['id']}/messages"
        )

    assert task.status_code == 201
    assert fetched_task.status_code == 200
    assert conversation.status_code == 201
    assert message.status_code == 201
    assert [item["text"] for item in messages.json()] == ["hello"]


def test_startup_rejects_database_without_agent_profiles(
    db_path: pathlib.Path,
) -> None:
    """A database migrated only to 0003 lacks ``agent_profiles``."""
    command.upgrade(alembic_config(db_path), "0003_create_turns")

    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(_app(db_path)):
            pass

    message = str(excinfo.value)
    assert "agent_profiles" in message
    assert "migration" in message.lower()

    # The application did not create schema while failing to start.
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "tasks" in tables
    assert "agent_profiles" not in tables


def test_delete_agent_archives_and_filters_from_list(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        _create(client, agent_id="temp-agent", name="Temporary")
        _create(client, agent_id="perm-agent", name="Permanent")

        # Initial list has both
        agents = client.get("/agents").json()
        assert len(agents) == 2
        assert {a["agent_id"] for a in agents} == {"temp-agent", "perm-agent"}

        # Archive temp-agent
        delete_resp = client.delete("/agents/temp-agent")
        assert delete_resp.status_code == 200
        assert delete_resp.json()["revision"] == 2
        assert delete_resp.json()["runner_config"]["archived"] is True

        # Default list excludes archived
        active_agents = client.get("/agents").json()
        assert len(active_agents) == 1
        assert active_agents[0]["agent_id"] == "perm-agent"

        # List with include_archived=true includes both
        all_agents = client.get("/agents?include_archived=true").json()
        assert len(all_agents) == 2
        assert {a["agent_id"] for a in all_agents} == {"temp-agent", "perm-agent"}


def test_delete_system_agent_rejected(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        _create(client, agent_id="system-secretary", name="Secretary")
        resp = client.delete("/agents/system-secretary")
        assert resp.status_code == 400
        assert "cannot be archived" in resp.json()["detail"]

        # Custom system agent can be archived
        _create(client, agent_id="system-custom", name="Custom System")
        resp_custom = client.delete("/agents/system-custom")
        assert resp_custom.status_code == 200
        assert resp_custom.json()["runner_config"]["archived"] is True


