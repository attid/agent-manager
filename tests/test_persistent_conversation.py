"""Acceptance tests for the persistent conversation Pi handler.

The handler is exercised against the real migrated file database, the real
runtime binding repository, the real profile export factory and a real child
Python process (``tests/fake_pi_runner.py``). No model, network or credential
is involved. Child launches happen outside every database transaction.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from agent_manager.persistent_conversation import PersistentConversationHandler
from agent_manager.runtime_defaults import RuntimeDefaultsResolver
from agent_manager.secretary import TurnInput, TurnProcessor
from agent_manager.storage import (
    AgentProfileRepository,
    ConversationRepository,
    ConversationRuntimeBusyError,
    ConversationRuntimeRepository,
    Database,
)
from agent_manager.storage.models import ConversationRuntime

FAKE_RUNNER = pathlib.Path(__file__).with_name("fake_pi_runner.py")


def _base_runner_config(
    *,
    session_id: str = "triage-session",
    mode: str = "success",
    summary: str = "frozen summary",
    omit_session_id: bool = False,
    argv_log: pathlib.Path | None = None,
    instructions_probe: pathlib.Path | None = None,
    selection_probe: pathlib.Path | None = None,
    state_dir: pathlib.Path | None = None,
    stateful_session: bool = False,
) -> dict[str, object]:
    config: dict[str, object] = {
        "mode": mode,
        "summary_text": summary,
        "session_id_value": session_id,
        "omit_session_id": omit_session_id,
    }
    if argv_log is not None:
        config["argv_log"] = str(argv_log)
    if instructions_probe is not None:
        config["instructions_probe"] = str(instructions_probe)
    if selection_probe is not None:
        config["selection_probe"] = str(selection_probe)
    if state_dir is not None:
        config["state_dir"] = str(state_dir)
    if stateful_session:
        config["stateful_session"] = True
    return config


def _profile(
    database: Database,
    *,
    agent_id: str = "triage",
    prompt: str = "You are the triage agent.",
    runner_config: dict[str, object] | None = None,
):
    return AgentProfileRepository(database).create(
        agent_id=agent_id,
        name="Triage",
        role="worker",
        system_prompt=prompt,
        runner_config=(
            runner_config if runner_config is not None else {}
        ),
    )


def _defaults_source(
    artifact_root: pathlib.Path,
    *,
    app_config: pathlib.Path | None = None,
    global_settings: pathlib.Path | None = None,
) -> RuntimeDefaultsResolver:
    """A resolver bound to temporary files, never the real host settings.

    When a path is not supplied it points at a non-existent file under the
    test's ``tmp_path``, so resolution reads nothing and the real host Pi
    configuration is never consulted.
    """
    base = artifact_root.parent
    return RuntimeDefaultsResolver(
        app_config_path=(
            app_config if app_config is not None else base / "app.toml"
        ),
        global_settings_path=(
            global_settings
            if global_settings is not None
            else base / "pi-settings.json"
        ),
    )


def _handler(
    database: Database,
    artifact_root: pathlib.Path,
    *,
    agent_id: str = "triage",
    runtime_defaults: RuntimeDefaultsResolver | None = None,
) -> PersistentConversationHandler:
    return PersistentConversationHandler(
        database,
        agent_id,
        runner_argv_prefix=[sys.executable, str(FAKE_RUNNER)],
        artifact_root=artifact_root,
        runtime_defaults=(
            runtime_defaults
            if runtime_defaults is not None
            else _defaults_source(artifact_root)
        ),
    )


def _create_conversation(
    database: Database, *, conversation_id: str = "conv-1"
) -> None:
    ConversationRepository(database).create_conversation(id=conversation_id)


def _append_user(
    database: Database,
    *,
    conversation_id: str = "conv-1",
    text: str = "hello",
):
    return ConversationRepository(database).append_message(
        conversation_id=conversation_id,
        role="user",
        text=text,
        source="test",
    ).message


def _enqueue(
    processor: TurnProcessor,
    *,
    conversation_id: str,
    input_message_id: str,
) -> None:
    processor.enqueue(
        conversation_id=conversation_id,
        input_message_id=input_message_id,
    )


def _turn_input(turn: object) -> TurnInput:
    return TurnInput(
        turn_id=turn.id,  # type: ignore[attr-defined]
        conversation_id=turn.conversation_id,  # type: ignore[attr-defined]
        input_message_id=turn.input_message_id,  # type: ignore[attr-defined]
        role="user",
        text="hello",
        metadata={},
        attempt_count=turn.attempt_count,  # type: ignore[attr-defined]
    )


def _assistant_count(
    database: Database, *, conversation_id: str = "conv-1"
) -> int:
    return sum(
        1
        for message in ConversationRepository(database).list_messages(
            conversation_id
        )
        if message.role == "assistant"
    )


def _set_binding(
    database: Database,
    *,
    conversation_id: str = "conv-1",
    **values: object,
) -> None:
    with database.session() as session:
        session.execute(
            update(ConversationRuntime)
            .where(
                ConversationRuntime.conversation_id == conversation_id
            )
            .values(**values)
        )


# -- first run then resume -------------------------------------------------


def test_first_run_then_resume_reuses_session_and_role_export(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    argv_log = tmp_path / "argv.json"
    configured_state = tmp_path / "configured-runner-state"
    expected_state = configured_state.resolve()

    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                argv_log=argv_log,
                state_dir=configured_state,
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        handler = _handler(database, artifact_root)
        result = processor.run_once(handler, conversation_id="conv-1")
        assert result.status == "completed"
        assert result.reply is not None
        assert result.reply.text == "frozen summary"
        assert handler.last_execution is not None
        assert handler.last_execution.launch_mode == "run"

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.runner_session_id == "triage-session"
        assert binding.role_export_path is not None
        assert binding.snapshot_export_path is not None
        assert binding.prompt_export_path is not None
        assert binding.runner_state_dir == str(expected_state)
        first_role_export = binding.role_export_path
        first_state_dir = binding.runner_state_dir
        # The persisted state directory is exactly the generated role
        # config's trusted state_dir, not an invented sibling path.
        role_config = json.loads(
            pathlib.Path(binding.role_export_path).read_text(
                encoding="utf-8"
            )
        )
        assert (
            pathlib.Path(role_config["state_dir"]).expanduser().resolve()
            == expected_state
        )
        assert first_state_dir == str(expected_state)
        provenance = binding.last_provenance
        assert provenance is not None
        assert provenance["agent_id"] == "triage"
        assert provenance["profile_revision"] == 1
        assert provenance["role_export_path"] == first_role_export
        assert provenance["snapshot_export_path"] == binding.snapshot_export_path
        assert provenance["prompt_export_path"] == binding.prompt_export_path
        assert provenance["runner_state_dir"] == first_state_dir
        assert provenance["launch_mode"] == "run"
        assert provenance["source"] == "secretary"
        assert provenance["returned_session_id"] == "triage-session"

        first_reply_path = handler.last_execution.summary_path
        assert first_reply_path is not None
        first_reply_text = first_reply_path.read_text(encoding="utf-8")
        first_argv = json.loads(argv_log.read_text(encoding="utf-8"))["argv"]
        assert "run" in first_argv
        assert "resume" not in first_argv
    finally:
        database.dispose()

    # A brand-new database and handler object must reconstruct resume state
    # from the persisted binding alone.
    database = Database.from_path(migrated_db)
    try:
        processor = TurnProcessor(database)
        message = _append_user(database, text="second")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        handler = _handler(database, artifact_root)
        result = processor.run_once(handler, conversation_id="conv-1")
        assert result.status == "completed"
        assert result.reply is not None
        assert result.reply.text == "frozen summary"

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.runner_session_id == "triage-session"
        assert binding.role_export_path == first_role_export
        assert binding.runner_state_dir == first_state_dir
        assert binding.last_provenance["launch_mode"] == "resume"

        execution = handler.last_execution
        assert execution is not None
        assert execution.launch_mode == "resume"
        assert execution.requested_session_id == "triage-session"
        assert execution.returned_session_id == "triage-session"

        resume_argv = json.loads(argv_log.read_text(encoding="utf-8"))["argv"]
        assert "resume" in resume_argv
        assert "--state-dir" in resume_argv
        state_dir_arg = resume_argv[resume_argv.index("--state-dir") + 1]
        # The fake child reads the role JSON state_dir itself and only lets
        # the resume succeed when the argv value resolves to the same path;
        # this assertion records the comparison at the test boundary too.
        assert state_dir_arg == str(expected_state)
        assert state_dir_arg == first_state_dir
        assert "triage-session" in resume_argv

        # The first attempt's immutable reply evidence is untouched.
        assert first_reply_path.read_text(encoding="utf-8") == first_reply_text
        # No second frozen base export was created for the pinned
        # conversation. The fresh and resume calls each added exactly one
        # separate per-call execution role beside the pinned base export.
        assert len(list(artifact_root.rglob("snapshot.json"))) == 1
        assert (
            len(list(artifact_root.rglob("executions/*/runner_role.json")))
            == 2
        )
        assert _assistant_count(database) == 2
    finally:
        database.dispose()


# -- pinned revision survives registry edits ------------------------------


def test_registry_prompt_change_keeps_original_pinned_and_new_conversation_latest(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    probe_v1 = tmp_path / "probe-v1.json"
    probe_v2 = tmp_path / "probe-v2.json"

    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database, conversation_id="conv-a")
        _profile(
            database,
            prompt="PROMPT-V1",
            runner_config=_base_runner_config(
                session_id="s1",
                instructions_probe=probe_v1,
                state_dir=tmp_path / "state-a",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, conversation_id="conv-a", text="first")
        _enqueue(
            processor,
            conversation_id="conv-a",
            input_message_id=message.id,
        )
        handler = _handler(database, artifact_root)
        assert (
            processor.run_once(handler, conversation_id="conv-a").status
            == "completed"
        )
        binding_a = ConversationRuntimeRepository(database).get_binding(
            "conv-a"
        )
        assert binding_a is not None
        assert binding_a.profile_revision == 1
        export_a = binding_a.role_export_path
        prompt_a = pathlib.Path(binding_a.prompt_export_path)
        assert prompt_a.read_text(encoding="utf-8") == "PROMPT-V1"
    finally:
        database.dispose()

    database = Database.from_path(migrated_db)
    try:
        AgentProfileRepository(database).update(
            agent_id="triage",
            expected_revision=1,
            system_prompt="PROMPT-V2",
            runner_config=_base_runner_config(
                session_id="s2",
                instructions_probe=probe_v2,
                state_dir=tmp_path / "state-b",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)

        # Conversation A stays pinned to revision 1 and resumes it.
        message = _append_user(
            database, conversation_id="conv-a", text="second"
        )
        _enqueue(
            processor,
            conversation_id="conv-a",
            input_message_id=message.id,
        )
        handler_a = _handler(database, artifact_root)
        assert (
            processor.run_once(handler_a, conversation_id="conv-a").status
            == "completed"
        )
        assert handler_a.last_execution is not None
        assert handler_a.last_execution.launch_mode == "resume"
        assert handler_a.last_execution.profile_revision == 1
        assert handler_a.last_execution.returned_session_id == "s1"
        resolved_v1 = json.loads(probe_v1.read_text(encoding="utf-8"))
        assert any(
            "PROMPT-V1" in entry.get("text", "") for entry in resolved_v1
        )
        assert prompt_a.read_text(encoding="utf-8") == "PROMPT-V1"

        # A future conversation pins the latest revision 2.
        _create_conversation(database, conversation_id="conv-b")
        message_b = _append_user(
            database, conversation_id="conv-b", text="new"
        )
        _enqueue(
            processor,
            conversation_id="conv-b",
            input_message_id=message_b.id,
        )
        handler_b = _handler(database, artifact_root)
        assert (
            processor.run_once(handler_b, conversation_id="conv-b").status
            == "completed"
        )
        assert handler_b.last_execution is not None
        assert handler_b.last_execution.launch_mode == "run"
        assert handler_b.last_execution.profile_revision == 2
        assert handler_b.last_execution.returned_session_id == "s2"
        binding_b = ConversationRuntimeRepository(database).get_binding(
            "conv-b"
        )
        assert binding_b is not None
        assert binding_b.profile_revision == 2
        assert binding_b.role_export_path != export_a
        assert (
            pathlib.Path(binding_b.prompt_export_path).read_text(
                encoding="utf-8"
            )
            == "PROMPT-V2"
        )
        resolved_v2 = json.loads(probe_v2.read_text(encoding="utf-8"))
        assert any(
            "PROMPT-V2" in entry.get("text", "") for entry in resolved_v2
        )
    finally:
        database.dispose()


# -- fail-closed session handling -----------------------------------------


def test_first_run_without_returned_session_blocks_and_writes_no_assistant(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                omit_session_id=True,
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database)
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        result = processor.run_once(
            _handler(database, artifact_root), conversation_id="conv-1"
        )
        assert result.status == "failed"
        assert "session" in (result.error or "").lower()
        assert _assistant_count(database) == 0

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is not None
        assert binding.reservation_token is None
        assert binding.runner_session_id is None
    finally:
        database.dispose()


def test_resume_without_pinned_session_blocks_and_writes_no_assistant(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                session_id="s1",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        assert (
            processor.run_once(
                _handler(database, artifact_root), conversation_id="conv-1"
            ).status
            == "completed"
        )
        assert _assistant_count(database) == 1

        # Simulate a binding that lost its native session id.
        _set_binding(database, runner_session_id=None)

        message2 = _append_user(database, text="second")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message2.id,
        )
        result = processor.run_once(
            _handler(database, artifact_root), conversation_id="conv-1"
        )
        assert result.status == "failed"
        assert "session" in (result.error or "").lower()
        assert _assistant_count(database) == 1

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is not None
        assert binding.runner_session_id is None
    finally:
        database.dispose()


def test_mismatched_resume_blocks_and_writes_no_assistant(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                session_id="s1",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        assert (
            processor.run_once(
                _handler(database, artifact_root), conversation_id="conv-1"
            ).status
            == "completed"
        )
        assert _assistant_count(database) == 1

        # The pinned role export now reports a different native session.
        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        role_path = pathlib.Path(binding.role_export_path)
        role_config = json.loads(role_path.read_text(encoding="utf-8"))
        role_config["session_id_value"] = "different-session"
        role_path.write_text(json.dumps(role_config), encoding="utf-8")

        message2 = _append_user(database, text="second")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message2.id,
        )
        result = processor.run_once(
            _handler(database, artifact_root), conversation_id="conv-1"
        )
        assert result.status == "failed"
        assert _assistant_count(database) == 1

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is not None
        assert binding.runner_session_id == "s1"
    finally:
        database.dispose()


def test_failed_resume_blocks_and_writes_no_assistant(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                session_id="s1",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        assert (
            processor.run_once(
                _handler(database, artifact_root), conversation_id="conv-1"
            ).status
            == "completed"
        )
        assert _assistant_count(database) == 1

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        role_path = pathlib.Path(binding.role_export_path)
        role_config = json.loads(role_path.read_text(encoding="utf-8"))
        role_config["mode"] = "nonzero"
        role_path.write_text(json.dumps(role_config), encoding="utf-8")

        message2 = _append_user(database, text="second")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message2.id,
        )
        result = processor.run_once(
            _handler(database, artifact_root), conversation_id="conv-1"
        )
        assert result.status == "failed"
        assert _assistant_count(database) == 1

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is not None
        assert binding.runner_session_id == "s1"
    finally:
        database.dispose()


def test_relative_state_dir_is_rejected_before_launch_and_blocks(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                session_id="s1",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        assert (
            processor.run_once(
                _handler(database, artifact_root), conversation_id="conv-1"
            ).status
            == "completed"
        )

        _set_binding(database, runner_state_dir="relative/state")

        message2 = _append_user(database, text="second")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message2.id,
        )
        result = processor.run_once(
            _handler(database, artifact_root), conversation_id="conv-1"
        )
        assert result.status == "failed"
        assert "state_dir" in (result.error or "")
        assert _assistant_count(database) == 1

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is not None
        assert binding.runner_state_dir == "relative/state"
    finally:
        database.dispose()


# -- concurrency -----------------------------------------------------------


def test_two_owners_cannot_launch_twice(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    setup = Database.from_path(migrated_db)
    try:
        _create_conversation(setup)
        _profile(
            setup,
            runner_config=_base_runner_config(
                session_id="s1",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(setup)
        message = _append_user(setup)
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        turn = processor.claim_next(conversation_id="conv-1")
        assert turn is not None
        turn_input = _turn_input(turn)
    finally:
        setup.dispose()

    barrier = threading.Barrier(2)

    def owner() -> object:
        database = Database.from_path(migrated_db)
        try:
            handler = _handler(database, artifact_root)
            barrier.wait()
            try:
                return handler(turn_input)
            except ConversationRuntimeBusyError as error:
                return error
        finally:
            database.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [executor.submit(owner), executor.submit(owner)]
        results = [future.result() for future in outcomes]

    successes = [result for result in results if isinstance(result, str)]
    busy = [
        result
        for result in results
        if isinstance(result, ConversationRuntimeBusyError)
    ]
    assert len(successes) == 1
    assert len(busy) == 1
    # Exactly one child process wrote a per-execution task artifact.
    assert len(list(artifact_root.rglob("task.txt"))) == 1


# -- cached recovery -------------------------------------------------------


def test_cached_result_after_recovery_commits_one_assistant_without_second_child(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                session_id="s1",
                summary="cached answer",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        turn = processor.claim_next(conversation_id="conv-1")
        assert turn is not None

        # Launch once directly, storing the success on the binding but
        # "crashing" before TurnProcessor.complete runs.
        handler = _handler(database, artifact_root)
        assert handler(_turn_input(turn)) == "cached answer"
        assert handler.last_execution is not None
        assert handler.last_execution.launch_mode == "run"
        assert len(list(artifact_root.rglob("task.txt"))) == 1

        processor.recover_abandoned(turn.id, claim_token=turn.claim_token)

        # run_once claims the recovered turn; the handler returns the stored
        # reply without launching a second child.
        cached_handler = _handler(database, artifact_root)
        result = processor.run_once(cached_handler, conversation_id="conv-1")
        assert result.status == "completed"
        assert result.reply is not None
        assert result.reply.text == "cached answer"
        assert result.turn is not None
        assert result.turn.attempt_count == turn.attempt_count + 1
        assert cached_handler.last_execution is not None
        assert cached_handler.last_execution["launch_mode"] == "run"
        assert cached_handler.last_execution["returned_session_id"] == "s1"
        assert len(list(artifact_root.rglob("task.txt"))) == 1
        assert _assistant_count(database) == 1

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.runner_session_id == "s1"
        assert binding.reservation_token is None
    finally:
        database.dispose()


# -- no I/O on construction / transaction boundary ------------------------


def test_constructing_handler_launches_no_child(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        handler = _handler(database, artifact_root)
        assert handler.last_execution is None
        assert not artifact_root.exists()
    finally:
        database.dispose()


def test_child_launch_happens_outside_database_transactions(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path, monkeypatch
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                session_id="s1",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database)
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )

        active = {"count": 0}
        original_session = Database.session

        @contextlib.contextmanager
        def tracked_session(self):
            active["count"] += 1
            try:
                with original_session(self) as session:
                    yield session
            finally:
                active["count"] -= 1

        monkeypatch.setattr(Database, "session", tracked_session)

        observed: list[int] = []
        import agent_manager.pi_reply as pi_reply_module

        original_run = pi_reply_module.subprocess.run

        def checked_run(*args, **kwargs):
            observed.append(active["count"])
            return original_run(*args, **kwargs)

        monkeypatch.setattr(
            pi_reply_module.subprocess, "run", checked_run
        )

        result = processor.run_once(
            _handler(database, artifact_root), conversation_id="conv-1"
        )
        assert result.status == "completed"
        assert observed == [0]
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "state_dir_value",
    [
        pytest.param(None, id="missing"),
        pytest.param("relative/runner-state", id="relative"),
        pytest.param(123, id="non-string"),
    ],
)
def test_fresh_call_rejects_invalid_state_dir_before_child(
    tmp_path: pathlib.Path,
    migrated_db: pathlib.Path,
    state_dir_value: object,
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        runner_config = _base_runner_config()
        if state_dir_value is None:
            runner_config.pop("state_dir", None)
        else:
            runner_config["state_dir"] = state_dir_value
        _profile(database, runner_config=runner_config)
        processor = TurnProcessor(database)
        message = _append_user(database)
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        result = processor.run_once(
            _handler(database, artifact_root), conversation_id="conv-1"
        )
        assert result.status == "failed"
        assert "state_dir" in (result.error or "")
        assert _assistant_count(database) == 0
        # The trusted config was rejected before any child was launched.
        assert not list(artifact_root.rglob("task.txt"))

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is not None
        assert binding.runner_session_id is None
    finally:
        database.dispose()


def test_last_execution_reflects_adapter_failure_not_old_success(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                session_id="s1",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        handler = _handler(database, artifact_root)
        first = processor.run_once(handler, conversation_id="conv-1")
        assert first.status == "completed"
        first_record = handler.last_execution
        assert first_record is not None
        assert first_record.outcome == "completed"
        assert first_record.launch_mode == "run"

        # Make the pinned role config fail on the next (resume) call.
        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        role_path = pathlib.Path(binding.role_export_path)
        role_config = json.loads(role_path.read_text(encoding="utf-8"))
        role_config["mode"] = "failed_status"
        role_path.write_text(
            json.dumps(role_config), encoding="utf-8"
        )

        message2 = _append_user(database, text="second")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message2.id,
        )
        second = processor.run_once(handler, conversation_id="conv-1")
        assert second.status == "failed"
        failed_record = handler.last_execution
        # The failed invocation replaced the old success with the adapter's
        # actual failure record instead of leaving it as ``None``.
        assert failed_record is not None
        assert failed_record is not first_record
        assert failed_record.run_id != first_record.run_id
        assert failed_record.launch_mode == "resume"
        assert failed_record.outcome == "runner_failed"
        assert failed_record.error_type == "RunnerStatus"
        assert _assistant_count(database) == 1

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is not None
    finally:
        database.dispose()


# -- per-call runtime defaults --------------------------------------------


def _write_settings(
    path: pathlib.Path, *, provider: str, model: str
) -> pathlib.Path:
    path.write_text(
        json.dumps({"defaultProvider": provider, "defaultModel": model}),
        encoding="utf-8",
    )
    return path


def test_default_selection_refreshes_on_resume_and_pins_base_export(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    """Edit a temp defaults file between turns; a new handler sees it.

    The pinned base role, its snapshot and the first call's execution role
    and provenance stay unchanged, while the resumed call runs on the new
    inherited provider/model/auth over the same native session.
    """
    artifact_root = tmp_path / "exports"
    settings = tmp_path / "pi-settings.json"
    selection_probe = tmp_path / "selection.json"
    instructions_probe = tmp_path / "instructions.json"
    _write_settings(settings, provider="p1", model="m1")

    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            prompt="PINNED-PROMPT",
            runner_config=_base_runner_config(
                session_id="native-1",
                selection_probe=selection_probe,
                instructions_probe=instructions_probe,
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        first_handler = _handler(
            database,
            artifact_root,
            runtime_defaults=_defaults_source(
                artifact_root, global_settings=settings
            ),
        )
        first_result = processor.run_once(
            first_handler, conversation_id="conv-1"
        )
        assert first_result.status == "completed"
        first_record = first_handler.last_execution
        assert first_record is not None
        assert first_record.launch_mode == "run"
        # The child actually received the inherited selection.
        assert json.loads(selection_probe.read_text(encoding="utf-8")) == {
            "provider": "p1",
            "model": "m1",
            "pi_auth_provider": "p1",
            "models_file": None,
        }
        assert first_record.runtime_defaults is not None
        assert (
            first_record.runtime_defaults["selection"]["provider"][
                "source"
            ]
            == "global_pi"
        )
        first_execution_role = pathlib.Path(first_record.role_config_path)
        first_execution_text = first_execution_role.read_text(
            encoding="utf-8"
        )
        first_provenance_path = first_record.provenance_path
        first_provenance_text = first_provenance_path.read_text(
            encoding="utf-8"
        )

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        base_role_path = pathlib.Path(binding.role_export_path)
        base_role_text = base_role_path.read_text(encoding="utf-8")
        base_role_config = json.loads(base_role_text)
        # The pinned base policy carries no inherited selection.
        assert "provider" not in base_role_config
        assert "model" not in base_role_config
        snapshot_path = pathlib.Path(binding.snapshot_export_path)
        snapshot_text = snapshot_path.read_text(encoding="utf-8")
        # The execution role is a separate document beside the base export.
        assert first_execution_role != base_role_path
        assert (
            first_execution_role.parent.parent.parent
            == base_role_path.parent
        )
    finally:
        database.dispose()

    # Manual edit of the defaults file; a brand-new handler must see it.
    _write_settings(settings, provider="p2", model="m2")
    database = Database.from_path(migrated_db)
    try:
        processor = TurnProcessor(database)
        message = _append_user(database, text="second")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        resume_handler = _handler(
            database,
            artifact_root,
            runtime_defaults=_defaults_source(
                artifact_root, global_settings=settings
            ),
        )
        resume_result = processor.run_once(
            resume_handler, conversation_id="conv-1"
        )
        assert resume_result.status == "completed"
        resume_record = resume_handler.last_execution
        assert resume_record is not None
        assert resume_record.launch_mode == "resume"
        # Same native session and same pinned profile revision.
        assert resume_record.requested_session_id == "native-1"
        assert resume_record.returned_session_id == "native-1"
        assert resume_record.profile_revision == 1
        # The child actually saw the new inherited selection.
        assert json.loads(selection_probe.read_text(encoding="utf-8")) == {
            "provider": "p2",
            "model": "m2",
            "pi_auth_provider": "p2",
            "models_file": None,
        }
        resume_execution_role = pathlib.Path(
            resume_record.role_config_path
        )
        assert resume_execution_role != first_execution_role
        assert resume_execution_role != base_role_path

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        # Pinned base role, snapshot and first execution artifact unchanged.
        assert base_role_path.read_text(encoding="utf-8") == base_role_text
        assert snapshot_path.read_text(encoding="utf-8") == snapshot_text
        assert (
            first_execution_role.read_text(encoding="utf-8")
            == first_execution_text
        )
        assert json.loads(first_execution_text)["provider"] == "p1"
        assert (
            first_provenance_path.read_text(encoding="utf-8")
            == first_provenance_text
        )
        # Per-call provenance records the actual execution role and the
        # per-field selection evidence.
        provenance = binding.last_provenance
        assert provenance is not None
        assert provenance["execution_role_path"] == str(
            resume_execution_role
        )
        assert provenance["role_config_path"] == str(resume_execution_role)
        assert provenance["role_export_path"] == str(base_role_path)
        assert provenance["runtime_defaults"]["values"]["provider"] == "p2"
        assert (
            provenance["runtime_defaults"]["selection"]["model"]["source"]
            == "global_pi"
        )
        # Both turns reached the child with the pinned prompt.
        probe = json.loads(instructions_probe.read_text(encoding="utf-8"))
        assert any(
            entry.get("text") == "PINNED-PROMPT" for entry in probe
        )
        assert _assistant_count(database) == 2
        # One base export, one execution role per successful turn.
        assert len(list(artifact_root.rglob("snapshot.json"))) == 1
        assert (
            len(list(artifact_root.rglob("executions/*/runner_role.json")))
            == 2
        )
    finally:
        database.dispose()


def test_profile_fixed_override_wins_and_survives_resume(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    """An explicit profile provider/model wins over the temp defaults."""
    artifact_root = tmp_path / "exports"
    settings = tmp_path / "pi-settings.json"
    selection_probe = tmp_path / "selection.json"
    _write_settings(settings, provider="global-p", model="global-m")

    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config={
                "provider": "fixed-p",
                "model": "fixed-m",
                "mode": "success",
                "summary_text": "fixed summary",
                "session_id_value": "fixed-session",
                "selection_probe": str(selection_probe),
                "state_dir": str(tmp_path / "state"),
                "stateful_session": True,
            },
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        handler = _handler(
            database,
            artifact_root,
            runtime_defaults=_defaults_source(
                artifact_root, global_settings=settings
            ),
        )
        result = processor.run_once(handler, conversation_id="conv-1")
        assert result.status == "completed"
        record = handler.last_execution
        assert record is not None
        assert json.loads(selection_probe.read_text(encoding="utf-8")) == {
            "provider": "fixed-p",
            "model": "fixed-m",
            "pi_auth_provider": "fixed-p",
            "models_file": None,
        }
        assert record.runtime_defaults is not None
        assert (
            record.runtime_defaults["selection"]["provider"]["source"]
            == "role_config"
        )
        assert (
            record.runtime_defaults["selection"]["model"]["source"]
            == "role_config"
        )
    finally:
        database.dispose()

    # Changing the defaults file does not displace the fixed override.
    _write_settings(settings, provider="other-p", model="other-m")
    database = Database.from_path(migrated_db)
    try:
        processor = TurnProcessor(database)
        message = _append_user(database, text="second")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        handler = _handler(
            database,
            artifact_root,
            runtime_defaults=_defaults_source(
                artifact_root, global_settings=settings
            ),
        )
        result = processor.run_once(handler, conversation_id="conv-1")
        assert result.status == "completed"
        record = handler.last_execution
        assert record is not None
        assert record.launch_mode == "resume"
        assert record.returned_session_id == "fixed-session"
        assert json.loads(selection_probe.read_text(encoding="utf-8")) == {
            "provider": "fixed-p",
            "model": "fixed-m",
            "pi_auth_provider": "fixed-p",
            "models_file": None,
        }
    finally:
        database.dispose()


def test_invalid_defaults_release_then_repair_and_retry_runs_once(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    """A malformed defaults file releases the call; repair retries once."""
    artifact_root = tmp_path / "exports"
    settings = tmp_path / "pi-settings.json"
    settings.write_text("{not valid json", encoding="utf-8")
    selection_probe = tmp_path / "selection.json"

    database = Database.from_path(migrated_db)
    failed_turn_id = None
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                selection_probe=selection_probe,
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        result = processor.run_once(
            _handler(
                database,
                artifact_root,
                runtime_defaults=_defaults_source(
                    artifact_root, global_settings=settings
                ),
            ),
            conversation_id="conv-1",
        )
        assert result.status == "failed"
        assert result.turn is not None
        failed_turn_id = result.turn.id
        # Zero child calls: the failure is proven to be before the adapter.
        assert not list(artifact_root.rglob("task.txt"))
        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is None
        assert binding.reservation_token is None
        # Prepared paths were persisted before resolution and are reusable.
        assert binding.role_export_path is not None
        assert binding.runner_session_id is None
    finally:
        database.dispose()

    # Repair the file and explicitly retry the failed turn.
    _write_settings(settings, provider="ok-p", model="ok-m")
    database = Database.from_path(migrated_db)
    try:
        processor = TurnProcessor(database)
        assert failed_turn_id is not None
        processor.retry_failed(failed_turn_id)
        result = processor.run_once(
            _handler(
                database,
                artifact_root,
                runtime_defaults=_defaults_source(
                    artifact_root, global_settings=settings
                ),
            ),
            conversation_id="conv-1",
        )
        assert result.status == "completed"
        # Exactly one child call in total, with no duplicate model call.
        assert len(list(artifact_root.rglob("task.txt"))) == 1
        assert _assistant_count(database) == 1
        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is None
        assert binding.runner_session_id == "triage-session"
        # The aborted attempt wrote no execution role; the repaired run
        # reused the single pinned base export.
        assert len(list(artifact_root.rglob("snapshot.json"))) == 1
        assert (
            len(list(artifact_root.rglob("executions/*/runner_role.json")))
            == 1
        )
    finally:
        database.dispose()


def test_unknown_child_failure_remains_blocked_without_replay(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    """A failure after entering the child fails closed and never replays."""
    artifact_root = tmp_path / "exports"
    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                mode="nonzero",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        result = processor.run_once(
            _handler(database, artifact_root), conversation_id="conv-1"
        )
        assert result.status == "failed"
        assert result.turn is not None
        assert len(list(artifact_root.rglob("task.txt"))) == 1
        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.blocked_reason is not None
        assert binding.runner_session_id is None

        # An explicit retry of the failed turn cannot reach the child again.
        processor.retry_failed(result.turn.id)
        retried = processor.run_once(
            _handler(database, artifact_root), conversation_id="conv-1"
        )
        assert retried.status == "failed"
        assert len(list(artifact_root.rglob("task.txt"))) == 1
        assert _assistant_count(database) == 0
    finally:
        database.dispose()


def test_cached_recovery_ignores_broken_defaults_and_relaunches_nothing(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    """A cached successful result returns before any defaults resolution."""
    artifact_root = tmp_path / "exports"
    settings = tmp_path / "pi-settings.json"
    _write_settings(settings, provider="p1", model="m1")

    database = Database.from_path(migrated_db)
    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                session_id="s1",
                summary="cached answer",
                state_dir=tmp_path / "state",
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="first")
        _enqueue(
            processor,
            conversation_id="conv-1",
            input_message_id=message.id,
        )
        turn = processor.claim_next(conversation_id="conv-1")
        assert turn is not None
        handler = _handler(
            database,
            artifact_root,
            runtime_defaults=_defaults_source(
                artifact_root, global_settings=settings
            ),
        )
        assert handler(_turn_input(turn)) == "cached answer"
        assert len(list(artifact_root.rglob("task.txt"))) == 1
        assert (
            len(list(artifact_root.rglob("executions/*/runner_role.json")))
            == 1
        )
        processor.recover_abandoned(turn.id, claim_token=turn.claim_token)

        # Break the defaults source; the cached recovery must not read it.
        settings.write_text("{broken", encoding="utf-8")
        cached_handler = _handler(
            database,
            artifact_root,
            runtime_defaults=_defaults_source(
                artifact_root, global_settings=settings
            ),
        )
        result = processor.run_once(cached_handler, conversation_id="conv-1")
        assert result.status == "completed"
        assert result.reply is not None
        assert result.reply.text == "cached answer"
        assert cached_handler.last_execution is not None
        assert cached_handler.last_execution["launch_mode"] == "run"
        assert _assistant_count(database) == 1
    finally:
        database.dispose()


def test_session_created_agents_persisted_to_repository(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    artifact_root = tmp_path / "artifacts"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    session_id = "test-session-agent-create"
    pi_dir = state_dir / "sessions" / session_id / "pi"
    pi_dir.mkdir(parents=True)

    # Simulate created_agents.json output by policy.mjs
    agents_json = pi_dir / "created_agents.json"
    agents_json.write_text(json.dumps([
        {
            "agent_id": "project-scout",
            "name": "Project Scout",
            "role": "analyzer",
            "tools": ["read", "bash"],
            "skills": ["codespaces"],
            "system_prompt": "Scout prompt.",
        }
    ]), encoding="utf-8")

    try:
        _create_conversation(database)
        _profile(
            database,
            runner_config=_base_runner_config(
                session_id=session_id,
                summary="Agent created.",
                state_dir=state_dir,
                stateful_session=True,
            ),
        )
        processor = TurnProcessor(database)
        message = _append_user(database, text="create scout agent")
        _enqueue(processor, conversation_id="conv-1", input_message_id=message.id)
        turn = processor.claim_next(conversation_id="conv-1")
        assert turn is not None

        handler = _handler(database, artifact_root)
        reply = handler(_turn_input(turn))
        assert reply == "Agent created."

        # Verify created agent in repository
        agent_repo = AgentProfileRepository(database)
        scout = agent_repo.get_current("project-scout")
        assert scout is not None
        assert scout.name == "Project Scout"
        assert scout.role == "analyzer"
        assert scout.system_prompt == "Scout prompt."
        assert scout.skills == [{"skill": "codespaces"}]
        assert scout.runner_config.get("tools") == ["read", "bash"]
    finally:
        database.dispose()


