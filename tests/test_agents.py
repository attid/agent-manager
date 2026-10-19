"""Acceptance tests for immutable versioned agent-profile storage.

Every test runs against a real migrated temporary file database, never an
in-memory database.
"""

from __future__ import annotations

import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from sqlalchemy import inspect

from agent_manager.secretary import TurnProcessor
from agent_manager.storage import (
    AgentProfileNotFoundError,
    AgentProfileRepository,
    AgentProfileRevisionConflictError,
    ConversationRepository,
    Database,
    DuplicateAgentProfileError,
    TaskRepository,
)

from .conftest import alembic_config


def test_fresh_migration_creates_agent_profiles_schema(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        inspector = inspect(database.engine)
        assert "agent_profiles" in inspector.get_table_names()
        columns = {
            column["name"]
            for column in inspector.get_columns("agent_profiles")
        }
        assert columns == {
            "agent_id",
            "revision",
            "name",
            "role",
            "system_prompt",
            "runner_config",
            "skills",
            "knowledge",
            "created_at",
        }
        primary_key = inspector.get_pk_constraint("agent_profiles")
        assert primary_key["constrained_columns"] == ["agent_id", "revision"]
    finally:
        database.dispose()


def test_upgrade_from_0003_preserves_rows_and_adds_agent_profiles(
    db_path: pathlib.Path,
) -> None:
    """The 0004 revision is additive: existing durable rows must survive."""
    config = alembic_config(db_path)
    command.upgrade(config, "0003_create_turns")

    database = Database.from_path(db_path)
    try:
        # Prepare historical data using only columns present at this revision.
        with database.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO tasks (id, title, workspace) VALUES ('keep', 'keep me', 'proj-a')")
        conversations = ConversationRepository(database)
        conversations.create_conversation(id="conv-keep")
        message = conversations.append_message(
            conversation_id="conv-keep",
            role="user",
            text="hello",
            source="test",
            external_id="keep-1",
        ).message
        turn = TurnProcessor(database).enqueue(
            conversation_id="conv-keep", input_message_id=message.id
        ).turn
        assert "agent_profiles" not in inspect(database.engine).get_table_names()
    finally:
        database.dispose()

    command.upgrade(config, "head")

    database = Database.from_path(db_path)
    try:
        assert TaskRepository(database).get("keep") is not None
        conversations = ConversationRepository(database)
        assert conversations.get_conversation("conv-keep") is not None
        messages = conversations.list_messages("conv-keep")
        assert [stored.text for stored in messages] == ["hello"]
        assert TurnProcessor(database).get_turn(turn.id) is not None
        assert "agent_profiles" in inspect(database.engine).get_table_names()

        # The new boundary is usable immediately after the upgrade.
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id="after-upgrade",
            name="After Upgrade",
            role="worker",
            system_prompt="be useful",
        )
        assert profiles.get_current("after-upgrade").revision == 1
    finally:
        database.dispose()


def test_create_returns_revision_one_and_reopens(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    profiles = AgentProfileRepository(database)
    created = profiles.create(
        agent_id="agent-1",
        name="First",
        role="worker",
        system_prompt="You are helpful.",
        runner_config={"model": "x"},
        skills=[{"skill": "search"}],
        knowledge={"topic": "math"},
    )
    assert created.revision == 1
    assert created.agent_id == "agent-1"
    assert created.created_at is not None
    database.dispose()

    reopened = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(reopened)
        current = profiles.get_current("agent-1")
        assert current is not None
        assert current.name == "First"
        assert current.role == "worker"
        assert current.system_prompt == "You are helpful."
        revision = profiles.get_revision("agent-1", 1)
        assert revision is not None
        assert revision.created_at == current.created_at
    finally:
        reopened.dispose()


def test_prompt_config_skills_knowledge_round_trip(
    migrated_db: pathlib.Path,
) -> None:
    runner_config = {
        "command": "pi",
        "args": ["--json", {"nested": [1, 2, 3]}],
        "env": {"A": "1", "B": None},
        "flag": True,
        "threshold": 3.5,
    }
    skills = [
        {"skill": "search", "config": {"depth": 2}},
        {"skill": "write", "enabled": False},
        {"skill": "\u0440\u0443\u0441\u0441\u043a\u0438\u0439"},
    ]
    knowledge = {
        "notes": ["\u0441\u0442\u0440\u043e\u043a\u0430", "emoji \U0001f31f"],
        "counts": {"a": 1, "b": 2},
        "empty": {},
    }
    database = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id="round-trip",
            name="Round Trip",
            role="analyst",
            system_prompt="\u041f\u0440\u0438\u0432\u0435\u0442 \U0001f31f",
            runner_config=runner_config,
            skills=skills,
            knowledge=knowledge,
        )
        current = profiles.get_current("round-trip")
        assert current is not None
        assert current.system_prompt == "\u041f\u0440\u0438\u0432\u0435\u0442 \U0001f31f"
        assert current.runner_config == runner_config
        assert current.skills == skills
        assert current.knowledge == knowledge
    finally:
        database.dispose()


def test_arbitrary_roles_names_and_prompts(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(database)
        role = "a role not in any list / \u89d2\u8272 / \U0001f31f"
        name = "n" * 5_000
        prompt = "p" * 20_000
        profiles.create(
            agent_id="free-form",
            name=name,
            role=role,
            system_prompt=prompt,
        )
        current = profiles.get_current("free-form")
        assert current is not None
        assert current.name == name
        assert current.role == role
        assert current.system_prompt == prompt
    finally:
        database.dispose()


def test_update_appends_and_old_snapshot_is_immutable(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id="agent-1",
            name="Original",
            role="worker",
            system_prompt="original prompt",
            runner_config={"seed": 1},
            skills=[{"skill": "one"}],
            knowledge={"k": "v"},
        )
        updated = profiles.update(
            agent_id="agent-1", expected_revision=1, name="Renamed"
        )
        assert updated.revision == 2
        assert updated.name == "Renamed"

        original = profiles.get_revision("agent-1", 1)
        assert original is not None
        assert original.name == "Original"
        assert original.system_prompt == "original prompt"
        assert original.runner_config == {"seed": 1}
        assert original.skills == [{"skill": "one"}]
        assert original.knowledge == {"k": "v"}

        history = profiles.list_history("agent-1")
        assert [profile.revision for profile in history] == [1, 2]
        assert history[0].name == "Original"
        assert history[1].name == "Renamed"
    finally:
        database.dispose()


def test_update_preserves_omitted_and_replaces_supplied_fields(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id="agent-1",
            name="Original",
            role="worker",
            system_prompt="original prompt",
            runner_config={"seed": 1},
            skills=[{"skill": "one"}],
            knowledge={"k": "v"},
        )
        renamed = profiles.update(
            agent_id="agent-1", expected_revision=1, name="Renamed"
        )
        assert renamed.role == "worker"
        assert renamed.system_prompt == "original prompt"
        assert renamed.runner_config == {"seed": 1}
        assert renamed.skills == [{"skill": "one"}]
        assert renamed.knowledge == {"k": "v"}

        replaced = profiles.update(
            agent_id="agent-1",
            expected_revision=2,
            role="reviewer",
            system_prompt="second prompt",
            runner_config={"seed": 2},
            skills=[{"skill": "two"}, {"skill": "three"}],
            knowledge={"k": "v2"},
        )
        assert replaced.name == "Renamed"  # omitted, preserved across revisions
        assert replaced.role == "reviewer"
        assert replaced.system_prompt == "second prompt"
        assert replaced.runner_config == {"seed": 2}
        assert replaced.skills == [{"skill": "two"}, {"skill": "three"}]
        assert replaced.knowledge == {"k": "v2"}
    finally:
        database.dispose()


def test_list_current_returns_latest_per_agent(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id="b",
            name="B1",
            role="worker",
            system_prompt="b",
        )
        profiles.create(
            agent_id="a",
            name="A1",
            role="worker",
            system_prompt="a",
        )
        profiles.update(agent_id="a", expected_revision=1, name="A2")

        current = profiles.list_current()
        assert [(profile.agent_id, profile.revision) for profile in current] == [
            ("a", 2),
            ("b", 1),
        ]
        assert [profile.name for profile in current] == ["A2", "B1"]

        assert profiles.list_history("missing") == []
        assert len(profiles.list_history("a")) == 2
    finally:
        database.dispose()


def test_duplicate_creation_raises_and_leaves_original(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id="agent-1",
            name="Original",
            role="worker",
            system_prompt="original",
        )
        with pytest.raises(DuplicateAgentProfileError) as excinfo:
            profiles.create(
                agent_id="agent-1",
                name="Second",
                role="worker",
                system_prompt="second",
            )
        assert excinfo.value.agent_id == "agent-1"
        assert profiles.get_current("agent-1").name == "Original"
        assert len(profiles.list_history("agent-1")) == 1
    finally:
        database.dispose()


def test_update_missing_agent_raises_not_found(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(database)
        with pytest.raises(AgentProfileNotFoundError) as excinfo:
            profiles.update(
                agent_id="ghost", expected_revision=1, name="Nope"
            )
        assert excinfo.value.agent_id == "ghost"
    finally:
        database.dispose()


def test_update_stale_revision_conflicts_and_preserves_store(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id="agent-1",
            name="Original",
            role="worker",
            system_prompt="original",
        )
        profiles.update(agent_id="agent-1", expected_revision=1, name="Second")

        with pytest.raises(AgentProfileRevisionConflictError) as excinfo:
            profiles.update(
                agent_id="agent-1", expected_revision=1, name="Stale"
            )
        assert excinfo.value.agent_id == "agent-1"
        assert excinfo.value.expected_revision == 1
        assert excinfo.value.actual_revision == 2

        assert profiles.get_current("agent-1").name == "Second"
        assert [profile.name for profile in profiles.list_history("agent-1")] == [
            "Original",
            "Second",
        ]
    finally:
        database.dispose()


def test_concurrent_updates_same_expected_revision_one_commits(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id="contended",
            name="Base",
            role="worker",
            system_prompt="base",
        )

        workers = 8
        barrier = threading.Barrier(workers)

        def attempt(index: int) -> tuple[str, object]:
            barrier.wait()
            try:
                profiles.update(
                    agent_id="contended",
                    expected_revision=1,
                    name=f"worker-{index}",
                )
                return ("committed", index)
            except AgentProfileRevisionConflictError as error:
                return ("conflict", error)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(attempt, range(workers)))

        committed = [r for r in results if r[0] == "committed"]
        conflicts = [r for r in results if r[0] == "conflict"]
        assert len(committed) == 1
        assert len(conflicts) == workers - 1
        for _, error in conflicts:
            assert error.actual_revision == 2

        current = profiles.get_current("contended")
        assert current is not None
        assert current.revision == 2
        assert current.name == f"worker-{committed[0][1]}"
        history = profiles.list_history("contended")
        assert [profile.revision for profile in history] == [1, 2]
    finally:
        database.dispose()
