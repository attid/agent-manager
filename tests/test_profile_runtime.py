"""Acceptance tests for freezing registry profiles into Pi reply handlers.

The tests use the real migrated file database, the real profile repository,
the real ``TurnProcessor`` and a real child Python process
(``tests/fake_pi_runner.py``). No model, network, bwrap-in-bwrap or real
credential is involved.
"""

from __future__ import annotations

import json
import pathlib
import sys
import uuid

import pytest

from agent_manager.pi_reply import PiReplyHandler
from agent_manager.profile_runtime import (
    FrozenProfileHandler,
    ProfileRuntimeConfigurationError,
    ProfileSnapshotNotFoundError,
    freeze_profile_handler,
)
from agent_manager.secretary import TurnInput, TurnProcessor
from agent_manager.storage import (
    AgentProfileNotFoundError,
    AgentProfileRepository,
    ConversationRepository,
    Database,
)

FAKE_RUNNER = pathlib.Path(__file__).with_name("fake_pi_runner.py")


def _profile(
    repository: AgentProfileRepository,
    *,
    agent_id: str = "triage",
    prompt: str = "You are the triage agent.",
    runner_config: dict[str, object] | None = None,
    skills: list[dict[str, object]] | None = None,
    knowledge: dict[str, object] | None = None,
):
    return repository.create(
        agent_id=agent_id,
        name="Triage",
        role="worker",
        system_prompt=prompt,
        runner_config=(
            runner_config
            if runner_config is not None
            else {"mode": "success", "summary_text": "frozen summary"}
        ),
        skills=skills if skills is not None else [{"skill": "search"}],
        knowledge=knowledge if knowledge is not None else {"topic": "math"},
    )


def _freeze(
    database: Database,
    *,
    agent_id: str = "triage",
    artifact_root: pathlib.Path,
    revision: int | None = None,
) -> FrozenProfileHandler:
    return freeze_profile_handler(
        database,
        agent_id,
        revision=revision,
        runner_argv_prefix=[sys.executable, str(FAKE_RUNNER)],
        artifact_root=artifact_root,
    )


def _turn_input(
    *,
    text: str = "hello",
    turn_id: str = "turn-1",
    conversation_id: str = "conv-1",
    input_message_id: str = "message-1",
    attempt_count: int = 1,
) -> TurnInput:
    return TurnInput(
        turn_id=turn_id,
        conversation_id=conversation_id,
        input_message_id=input_message_id,
        role="user",
        text=text,
        metadata={},
        attempt_count=attempt_count,
    )


def _enqueued_turn(
    database: Database, *, conversation_id: str = "conv-1", text: str = "ping"
):
    repository = ConversationRepository(database)
    repository.create_conversation(id=conversation_id)
    message = repository.append_message(
        conversation_id=conversation_id,
        role="user",
        text=text,
        source="test",
    ).message
    processor = TurnProcessor(database)
    enqueued = processor.enqueue(
        conversation_id=conversation_id, input_message_id=message.id
    )
    return processor, repository, enqueued.turn


# -- export contents and role generation ---------------------------------


def test_freeze_exports_snapshot_prompt_and_preserved_instructions(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    existing_instruction = tmp_path / "existing-instructions.md"
    existing_text = "keep this trusted instruction"
    existing_instruction.write_text(existing_text, encoding="utf-8")
    argv_log = tmp_path / "argv.json"
    instructions_probe = tmp_path / "instructions.json"

    runner_config = {
        "mode": "success",
        "summary_text": "frozen answer",
        "instructions": [str(existing_instruction)],
        "argv_log": str(argv_log),
        "instructions_probe": str(instructions_probe),
        # This stored identity is deliberately wrong and must be overridden.
        "agent_id": "spoofed-id",
        "model": {"provider_ref": "opaque:selected-provider"},
    }
    skills = [{"skill": "search", "config": {"depth": 2}}]
    knowledge = {"notes": ["project fact"], "count": 2}
    prompt = "You triage incoming work.\nSecond line \u00e9\u4e2d"

    database = Database.from_path(migrated_db)
    try:
        repository = AgentProfileRepository(database)
        _profile(
            repository,
            prompt=prompt,
            runner_config=runner_config,
            skills=skills,
            knowledge=knowledge,
        )

        frozen = _freeze(database, artifact_root=tmp_path / "exports")

        # The wrapper is the Handler contract and keeps the existing adapter.
        assert isinstance(frozen, FrozenProfileHandler)
        assert isinstance(frozen.handler, PiReplyHandler)
        assert callable(frozen)
        assert frozen.agent_id == "triage"
        assert frozen.revision == 1
        assert frozen.version == 1
        assert frozen.config_path == frozen.role_config_path
        assert frozen.artifact_dir == frozen.export_dir / "runs"

        # Unique UUID directory, not derived from the agent or user input.
        uuid.UUID(frozen.export_dir.name)
        assert len(frozen.export_dir.name) == 32
        assert frozen.export_dir.parent == (tmp_path / "exports").absolute()
        assert "triage" not in frozen.export_dir.name

        # The exact stored prompt and generated role config are exported.
        assert frozen.prompt_path.read_text(encoding="utf-8") == prompt
        role = json.loads(frozen.role_config_path.read_text(encoding="utf-8"))
        assert role["agent_id"] == "triage"
        assert role["instructions"] == [
            str(existing_instruction),
            str(frozen.prompt_path),
        ]
        assert role["model"] == {"provider_ref": "opaque:selected-provider"}
        assert role["mode"] == "success"
        assert role["summary_text"] == "frozen answer"

        # The stored JSON is unchanged, including the now-overridden values.
        stored = repository.get_revision("triage", 1)
        assert stored is not None
        assert stored.runner_config == runner_config
        assert stored.system_prompt == prompt

        snapshot = json.loads(frozen.snapshot_path.read_text(encoding="utf-8"))
        assert snapshot["agent_id"] == "triage"
        assert snapshot["revision"] == 1
        assert snapshot["system_prompt"] == prompt
        assert snapshot["runner_config"] == runner_config
        assert snapshot["skills"] == skills
        assert snapshot["knowledge"] == knowledge
        assert snapshot["bindings"]["skills"] == skills
        assert snapshot["bindings"]["knowledge"] == knowledge
        assert snapshot["bindings"]["connected"] is False
        assert snapshot["bindings"]["not_connected"] == ["skills", "knowledge"]
        assert snapshot["runtime"]["role_config_path"] == str(
            frozen.role_config_path
        )

        # Declarative only: exported and reported, never connected or loaded.
        assert frozen.skills_connected is False
        assert frozen.knowledge_connected is False
        assert frozen.not_connected == ("skills", "knowledge")
        assert "skills" not in role
        assert "knowledge" not in role

        # Building the handler creates the run directory but launches nothing.
        assert frozen.runs_dir.is_dir()
        assert not argv_log.exists()
        assert not instructions_probe.exists()

        result = frozen(_turn_input(text="hello"))
        assert result == "frozen answer"

        # The generated prompt content reached the child's role instructions,
        # and the pre-existing trusted instruction was retained first.
        probe = json.loads(instructions_probe.read_text(encoding="utf-8"))
        assert probe == [
            {"path": str(existing_instruction), "text": existing_text},
            {"path": str(frozen.prompt_path), "text": prompt},
        ]
        logged_argv = json.loads(argv_log.read_text(encoding="utf-8"))["argv"]
        assert logged_argv[3] == str(frozen.role_config_path)

        record = frozen.last_execution
        assert record is not None
        assert record.outcome == "completed"
        assert record.profile_agent_id == "triage"
        assert record.profile_revision == 1
        assert record.role_config_path == frozen.role_config_path
        assert record.profile_snapshot_path == frozen.snapshot_path
        on_disk = json.loads(record.provenance_path.read_text(encoding="utf-8"))
        assert on_disk["profile_agent_id"] == "triage"
        assert on_disk["profile_revision"] == 1
        assert on_disk["role_config_path"] == str(frozen.role_config_path)
        assert on_disk["profile_snapshot_path"] == str(frozen.snapshot_path)
    finally:
        database.dispose()


# -- freezing across registry updates ------------------------------------


def test_freeze_appends_extra_instruction_documents(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    extra = tmp_path / "platform_discovery.md"
    extra.write_text("# Discovery\n", encoding="utf-8")
    try:
        repository = AgentProfileRepository(database)
        _profile(repository, prompt="identity", runner_config={})
        frozen = freeze_profile_handler(
            database,
            "triage",
            runner_argv_prefix=[sys.executable, str(FAKE_RUNNER)],
            artifact_root=tmp_path / "exports",
            extra_instructions=[extra],
        )
        role = json.loads(frozen.role_config_path.read_text(encoding="utf-8"))
        assert role["instructions"] == [str(frozen.prompt_path), str(extra)]
        # The stored snapshot never gains the injected discovery path.
        assert frozen.profile.runner_config == {}
    finally:
        database.dispose()


def test_old_handler_stays_revision_one_while_new_handler_gets_revision_two(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        repository = AgentProfileRepository(database)
        _profile(
            repository,
            prompt="revision one prompt",
            runner_config={
                "mode": "success",
                "summary_text": "revision one summary",
            },
            skills=[{"skill": "one"}],
            knowledge={"revision": 1},
        )
        first = _freeze(database, artifact_root=artifact_root)
        assert first.revision == 1
        assert first(_turn_input()) == "revision one summary"

        updated = repository.update(
            agent_id="triage",
            expected_revision=1,
            system_prompt="revision two prompt",
            runner_config={
                "mode": "success",
                "summary_text": "revision two summary",
            },
            skills=[{"skill": "two"}],
            knowledge={"revision": 2},
        )
        assert updated.revision == 2

        second = _freeze(database, artifact_root=artifact_root)
        assert second.revision == 2

        # The already-built handler and its export are unchanged.
        assert first.profile.revision == 1
        assert first.prompt_path.read_text(encoding="utf-8") == (
            "revision one prompt"
        )
        assert first(_turn_input()) == "revision one summary"
        first_snapshot = json.loads(
            first.snapshot_path.read_text(encoding="utf-8")
        )
        assert first_snapshot["revision"] == 1
        assert first_snapshot["skills"] == [{"skill": "one"}]
        assert first_snapshot["knowledge"] == {"revision": 1}

        # The new handler was built from revision two and never reuses exports.
        assert second.profile.revision == 2
        assert second.prompt_path.read_text(encoding="utf-8") == (
            "revision two prompt"
        )
        assert second(_turn_input()) == "revision two summary"
        second_snapshot = json.loads(
            second.snapshot_path.read_text(encoding="utf-8")
        )
        assert second_snapshot["revision"] == 2
        assert second_snapshot["skills"] == [{"skill": "two"}]
        assert second_snapshot["knowledge"] == {"revision": 2}

        assert first.export_dir != second.export_dir
        assert first.export_dir.is_dir()
        assert second.export_dir.is_dir()
        assert first.snapshot_path.exists()
        assert second.snapshot_path.exists()
        assert first.role_config_path.exists()
        assert second.role_config_path.exists()

        # Both execution artifact sets survived, each under its own export.
        first_runs = [path for path in first.runs_dir.iterdir() if path.is_dir()]
        second_runs = [
            path for path in second.runs_dir.iterdir() if path.is_dir()
        ]
        assert len(first_runs) == 2
        assert len(second_runs) == 1
        assert set(first_runs).isdisjoint(second_runs)
        assert first.last_execution is not None
        assert first.last_execution.profile_revision == 1
        assert second.last_execution is not None
        assert second.last_execution.profile_revision == 2
    finally:
        database.dispose()


def test_repeated_freeze_creates_unique_exports_without_overwriting(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        repository = AgentProfileRepository(database)
        _profile(repository, prompt="same prompt")
        first = _freeze(database, artifact_root=artifact_root)
        second = _freeze(database, artifact_root=artifact_root)

        assert first.export_dir != second.export_dir
        first_snapshot = json.loads(
            first.snapshot_path.read_text(encoding="utf-8")
        )
        second_snapshot = json.loads(
            second.snapshot_path.read_text(encoding="utf-8")
        )
        assert first_snapshot["agent_id"] == second_snapshot["agent_id"]
        assert first_snapshot["revision"] == second_snapshot["revision"] == 1
        assert first_snapshot["runner_config"] == second_snapshot["runner_config"]
        assert first_snapshot["skills"] == second_snapshot["skills"]
        assert first_snapshot["knowledge"] == second_snapshot["knowledge"]
        assert first.prompt_path.read_text(encoding="utf-8") == "same prompt"
        assert second.prompt_path.read_text(encoding="utf-8") == "same prompt"
        # Both full export sets remain readable after the newer export.
        assert first.snapshot_path.exists()
        assert second.snapshot_path.exists()
        assert first.role_config_path.exists()
        assert second.role_config_path.exists()
    finally:
        database.dispose()


# -- typed lookup and configuration errors -------------------------------


def test_unknown_profile_and_revision_raise_typed_errors(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        with pytest.raises(ProfileSnapshotNotFoundError) as missing:
            _freeze(database, agent_id="absent", artifact_root=tmp_path / "x")
        assert missing.value.agent_id == "absent"
        assert missing.value.revision is None
        assert isinstance(missing.value, AgentProfileNotFoundError)
        assert not (tmp_path / "x").exists()

        _profile(AgentProfileRepository(database))

        with pytest.raises(ProfileSnapshotNotFoundError) as revision:
            _freeze(database, artifact_root=tmp_path / "x", revision=99)
        assert revision.value.agent_id == "triage"
        assert revision.value.revision == 99
        assert "99" in str(revision.value)
        assert not (tmp_path / "x").exists()
    finally:
        database.dispose()


def test_non_object_runner_config_is_a_typed_error(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = AgentProfileRepository(database)
        repository.create(
            agent_id="bad-config",
            name="Bad",
            role="worker",
            system_prompt="prompt",
        )
        repository.update(
            agent_id="bad-config",
            expected_revision=1,
            runner_config=[],  # type: ignore[arg-type]
        )
        with pytest.raises(ProfileRuntimeConfigurationError):
            _freeze(
                database,
                agent_id="bad-config",
                artifact_root=tmp_path / "exports",
            )
        assert not (tmp_path / "exports").exists()
    finally:
        database.dispose()


# -- integration with the real TurnProcessor -----------------------------


def test_frozen_handler_completes_a_real_turn(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(
            AgentProfileRepository(database),
            runner_config={
                "mode": "success",
                "summary_text": "answer from frozen profile",
            },
        )
        frozen = _freeze(database, artifact_root=tmp_path / "exports")
        processor, repository, turn = _enqueued_turn(database)

        result = processor.run_once(frozen)

        assert result.status == "completed"
        assert result.turn is not None
        assert result.turn.id == turn.id
        messages = repository.list_messages("conv-1")
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[1].text == "answer from frozen profile"
        assert frozen.last_execution is not None
        assert frozen.last_execution.outcome == "completed"
        assert frozen.last_execution.profile_agent_id == "triage"
        assert frozen.last_execution.profile_revision == 1
    finally:
        database.dispose()


def test_frozen_handler_failure_is_recorded_with_profile_link(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(
            AgentProfileRepository(database),
            runner_config={
                "mode": "failed_status",
                "stderr_text": "SECRET_TOKEN_STDERR",
            },
        )
        frozen = _freeze(database, artifact_root=tmp_path / "exports")
        processor, repository, _ = _enqueued_turn(database)

        result = processor.run_once(frozen)

        assert result.status == "failed"
        assert result.turn is not None
        assert result.turn.reply_message_id is None
        assert result.error is not None
        assert "PiReplyFailedError" in result.error
        assert "SECRET_TOKEN_STDERR" not in result.error
        assert [
            message.role for message in repository.list_messages("conv-1")
        ] == ["user"]

        record = frozen.last_execution
        assert record is not None
        assert record.outcome == "runner_failed"
        assert record.profile_agent_id == "triage"
        assert record.profile_revision == 1
        assert record.role_config_path == frozen.role_config_path
        assert str(record.artifact_dir) in result.error
    finally:
        database.dispose()
