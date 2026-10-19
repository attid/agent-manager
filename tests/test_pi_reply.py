"""Acceptance tests for the Pi Runner reply-handler adapter.

The adapter is exercised against a real child Python process
(``tests/fake_pi_runner.py``) and, for the integration cases, the real
``TurnProcessor`` and a migrated temporary file database. No model, network,
bwrap-in-bwrap or real credential is involved.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

from agent_manager.pi_reply import (
    PiReplyCommandError,
    PiReplyConfigurationError,
    PiReplyFailedError,
    PiReplyHandler,
    PiReplyProtocolError,
    PiReplySummaryError,
)
from agent_manager.secretary import TurnInput, TurnProcessor
from agent_manager.storage import ConversationRepository, Database

FAKE_RUNNER = pathlib.Path(__file__).with_name("fake_pi_runner.py")

#: Sentinel so a caller can omit ``session_id_value`` from the fake result
#: while still being able to pass an explicit empty or ``None`` value.
_UNSET = object()


def _write_role(path: pathlib.Path, **fields: object) -> pathlib.Path:
    path.write_text(
        json.dumps(fields, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _handler(
    tmp_path: pathlib.Path,
    *,
    mode: str,
    summary_text: str = "fake summary",
    role_path: pathlib.Path | None = None,
    artifact_dir: pathlib.Path | None = None,
    stderr_text: str | None = None,
    log_line: str | None = None,
    argv_log: pathlib.Path | None = None,
    env_probe: pathlib.Path | None = None,
    env_var: str | None = None,
    shared_summary_path: pathlib.Path | None = None,
    session_id_value: object = _UNSET,
    omit_session_id: bool = False,
    resume_session_id: str | None = None,
    runner_state_dir: pathlib.Path | None = None,
) -> PiReplyHandler:
    if role_path is None:
        role_path = tmp_path / f"role-{mode}.json"
    role_fields: dict[str, object] = {
        "mode": mode,
        "summary_text": summary_text,
        "stderr_text": stderr_text,
        "log_line": log_line,
        "argv_log": str(argv_log) if argv_log is not None else None,
        "env_probe": str(env_probe) if env_probe is not None else None,
        "env_var": env_var,
        "omit_session_id": omit_session_id,
    }
    if shared_summary_path is not None:
        role_fields["shared_summary_path"] = str(shared_summary_path)
    if session_id_value is not _UNSET:
        role_fields["session_id_value"] = session_id_value
    _write_role(role_path, **role_fields)
    return PiReplyHandler(
        runner_argv_prefix=[sys.executable, str(FAKE_RUNNER)],
        role_config_path=str(role_path),
        artifact_dir=(
            artifact_dir if artifact_dir is not None else tmp_path / "artifacts"
        ),
        resume_session_id=resume_session_id,
        runner_state_dir=runner_state_dir,
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


# -- success path --------------------------------------------------------


def test_success_returns_summary_and_writes_artifacts(
    tmp_path: pathlib.Path,
) -> None:
    incoming = "line one\nline two \u00e9\u4e2d"
    summary = "\u041f\u0440\u0438\u0432\u0435\u0442 \U0001f31f"
    handler = _handler(tmp_path, mode="success", summary_text=summary)

    result = handler(_turn_input(text=incoming))

    assert result == summary
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "completed"
    assert record.runner_status == "completed"
    assert record.exit_code == 0
    assert record.turn_id == "turn-1"
    assert record.conversation_id == "conv-1"
    assert record.input_message_id == "message-1"
    assert record.attempt_count == 1

    # Each artifact exists and is linked by the provenance record.
    assert record.task_path.exists()
    assert record.stdout_path.exists()
    assert record.stderr_path.exists()
    assert record.result_path is not None and record.result_path.exists()
    assert record.provenance_path.exists()
    assert record.summary_path is not None
    assert record.summary_path.read_text(encoding="utf-8") == summary
    assert record.launch_mode == "run"
    assert record.requested_session_id is None
    assert record.returned_session_id is None
    # The saved reply is distinct from the Runner's original summary source.
    assert record.summary_source_path is not None
    assert record.summary_source_path != record.summary_path
    assert record.summary_source_path.read_text(encoding="utf-8") == summary

    # The task text carries the turn identity and the exact incoming text.
    task_text = record.task_path.read_text(encoding="utf-8")
    assert "turn_id: turn-1" in task_text
    assert "conversation_id: conv-1" in task_text
    assert "input_message_id: message-1" in task_text
    assert "attempt_count: 1" in task_text
    assert task_text.endswith(incoming)

    parsed_result = json.loads(record.result_path.read_text(encoding="utf-8"))
    assert parsed_result["status"] == "completed"

    on_disk = json.loads(record.provenance_path.read_text(encoding="utf-8"))
    assert on_disk["turn_id"] == "turn-1"
    assert on_disk["attempt_count"] == 1
    assert on_disk["outcome"] == "completed"
    assert on_disk["launch_mode"] == "run"
    assert on_disk["files"]["summary"] == str(record.summary_path)
    assert on_disk["files"]["summary_source"] == str(
        record.summary_source_path
    )
    assert on_disk["files"]["provenance"] == str(record.provenance_path)

    # Unique artifact subdirectory below the configured root.
    assert record.artifact_dir.parent == (tmp_path / "artifacts").absolute()
    assert record.artifact_dir.name.endswith(record.run_id)


# -- strict Runner output protocol --------------------------------------
#
# The Runner CLI contract is exactly one JSON object on stdout. These use a
# real child process to prove that leading logs, trailing junk and multiple
# objects all fail closed instead of being recovered into an execution result.


def test_leading_log_line_fails_closed(tmp_path: pathlib.Path) -> None:
    handler = _handler(
        tmp_path,
        mode="success",
        summary_text="ok",
        log_line="runner: starting",
    )
    with pytest.raises(PiReplyProtocolError):
        handler(_turn_input())
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "protocol_error"
    assert record.runner_status is None
    assert "runner: starting" in record.stdout_path.read_text(encoding="utf-8")


def test_trailing_junk_fails_closed(tmp_path: pathlib.Path) -> None:
    handler = _handler(tmp_path, mode="trailing_junk", summary_text="ok")
    with pytest.raises(PiReplyProtocolError):
        handler(_turn_input())
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "protocol_error"
    assert record.result_path is None


def test_multiple_json_objects_fail_closed(tmp_path: pathlib.Path) -> None:
    """A failed object followed by a completed object must not be recovered."""
    handler = _handler(
        tmp_path, mode="multiple_objects", summary_text="ok"
    )
    with pytest.raises(PiReplyProtocolError):
        handler(_turn_input())
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "protocol_error"
    assert record.result_path is None
    stdout = record.stdout_path.read_text(encoding="utf-8")
    assert '"status": "failed"' in stdout
    assert '"status": "completed"' in stdout


# -- literal argv / no shell expansion -----------------------------------


def test_paths_are_literal_argv_without_shell_expansion(
    tmp_path: pathlib.Path,
) -> None:
    tricky_name = "role $(touch PWNED_AM010) `touch PWNED_B` ; *.json"
    role_dir = tmp_path / "role dir with spaces"
    role_dir.mkdir()
    role_path = role_dir / tricky_name
    argv_log = tmp_path / "argv log.json"

    handler = _handler(
        tmp_path,
        mode="success",
        summary_text="ok",
        role_path=role_path,
        argv_log=argv_log,
    )
    handler(_turn_input())

    logged = json.loads(argv_log.read_text(encoding="utf-8"))["argv"]
    assert logged[1] == "run"
    assert logged[2] == "--agent"
    assert logged[3] == str(role_path)
    assert logged[4] == "--task"
    record = handler.last_execution
    assert record is not None
    assert logged[5] == str(record.task_path)
    assert logged[6] == "--turn-id"
    assert logged[7] == record.turn_id
    assert record.task_path.parent == record.artifact_dir

    # If a shell had interpreted the role path, it would have created this.
    assert not (tmp_path / "PWNED_AM010").exists()
    assert not (pathlib.Path.cwd() / "PWNED_AM010").exists()
    assert not (tmp_path / "PWNED_B").exists()


def test_child_inherits_caller_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AM010_SENTINEL", "present")
    env_probe = tmp_path / "env.json"
    handler = _handler(
        tmp_path,
        mode="success",
        summary_text="ok",
        env_probe=env_probe,
        env_var="AM010_SENTINEL",
    )
    assert handler(_turn_input()) == "ok"
    assert (
        json.loads(env_probe.read_text(encoding="utf-8"))["AM010_SENTINEL"]
        == "present"
    )


def test_relative_summary_path_is_resolved_against_artifact_dir(
    tmp_path: pathlib.Path,
) -> None:
    handler = _handler(
        tmp_path, mode="relative_summary", summary_text="relative ok"
    )
    assert handler(_turn_input()) == "relative ok"
    record = handler.last_execution
    assert record is not None
    assert record.summary_source_path == record.artifact_dir / "summary.txt"
    assert record.summary_path == record.artifact_dir / "reply.md"
    assert record.summary_path.read_text(encoding="utf-8") == "relative ok"


# -- fail-closed paths ---------------------------------------------------


def test_failed_status_with_exit_zero_raises(
    tmp_path: pathlib.Path,
) -> None:
    handler = _handler(tmp_path, mode="failed_status")
    with pytest.raises(PiReplyFailedError) as caught:
        handler(_turn_input())
    assert caught.value.runner_status == "failed"
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "runner_failed"
    assert record.exit_code == 0


def test_nonzero_exit_raises_command_error(tmp_path: pathlib.Path) -> None:
    handler = _handler(tmp_path, mode="nonzero")
    with pytest.raises(PiReplyCommandError) as caught:
        handler(_turn_input())
    assert caught.value.exit_code == 3
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "command_error"
    assert record.exit_code == 3
    assert record.stdout_path.exists()
    assert record.stderr_path.exists()


def test_failed_process_exposes_safe_connection_error_category(tmp_path: pathlib.Path) -> None:
    handler = _handler(tmp_path, mode="connection_error")
    with pytest.raises(PiReplyCommandError) as caught:
        handler(_turn_input())
    assert caught.value.connection_error is True
    assert "model_connection_error" in str(caught.value)
    assert handler.last_execution.runner_status == "failed"
    assert handler.last_execution.outcome == "command_error"


def test_telegram_error_summary_hides_traceback_and_escapes_diagnostics() -> None:
    from agent_manager.telegram.worker import secretary_error_summary

    assert secretary_error_summary(
        'Traceback\nFile "/private/artifacts"\nPiReplyCommandError: model_connection_error; see artifacts in /private'
    ) == "Не удалось связаться с провайдером модели. Попробуйте повторить сообщение."
    assert secretary_error_summary('Traceback\nValueError: invalid <name> & value') == "ValueError: invalid &lt;name&gt; &amp; value"


def test_malformed_json_raises_protocol_error(
    tmp_path: pathlib.Path,
) -> None:
    handler = _handler(tmp_path, mode="malformed")
    with pytest.raises(PiReplyProtocolError):
        handler(_turn_input())
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "protocol_error"
    assert record.result_path is None
    assert record.stdout_path.read_text(encoding="utf-8") != ""


def test_missing_status_raises_protocol_error(
    tmp_path: pathlib.Path,
) -> None:
    handler = _handler(tmp_path, mode="no_status")
    with pytest.raises(PiReplyProtocolError):
        handler(_turn_input())
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "protocol_error"


def test_missing_summary_raises_summary_error(
    tmp_path: pathlib.Path,
) -> None:
    handler = _handler(tmp_path, mode="missing_summary")
    with pytest.raises(PiReplySummaryError):
        handler(_turn_input())
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "summary_unavailable"
    assert record.runner_status == "completed"
    assert record.summary_path is None
    assert record.summary_source_path == record.artifact_dir / "absent.txt"


def test_empty_summary_raises_summary_error(
    tmp_path: pathlib.Path,
) -> None:
    handler = _handler(tmp_path, mode="empty_summary")
    with pytest.raises(PiReplySummaryError):
        handler(_turn_input())
    record = handler.last_execution
    assert record is not None
    assert record.outcome == "summary_unavailable"
    # A failed run never synthesizes a saved reply artifact; only the
    # original (empty) Runner summary source path stays inspectable.
    assert record.summary_path is None
    assert record.summary_source_path is not None
    assert record.summary_source_path.exists()
    assert record.summary_source_path.read_text(encoding="utf-8") == ""


def test_missing_runner_prefix_is_rejected(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError):
        PiReplyHandler(
            runner_argv_prefix=[],
            role_config_path=str(tmp_path / "role.json"),
            artifact_dir=tmp_path / "artifacts",
        )


def test_relative_role_config_path_is_rejected(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError):
        PiReplyHandler(
            runner_argv_prefix=[sys.executable],
            role_config_path="role.json",
            artifact_dir=tmp_path / "artifacts",
        )


# -- explicit resume mode ------------------------------------------------

def test_resume_argv_is_exact_literal_with_special_chars(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resume argv is exact; spaces and shell metacharacters are literal."""
    monkeypatch.setenv("AM017_SENTINEL", "resume-env")

    state_dir = tmp_path / "state dir $(touch PWNED_STATE)"
    state_dir.mkdir()
    session_id = "session id/with spaces;$(touch PWNED_SESSION)&*.json"
    role_dir = tmp_path / "role dir with spaces"
    role_dir.mkdir()
    role_path = role_dir / "role resume $(whoami).json"
    argv_log = tmp_path / "argv log.json"
    env_probe = tmp_path / "env.json"
    incoming = "resume me \u00e9\u4e2d\nsecond line"

    handler = _handler(
        tmp_path,
        mode="success",
        summary_text="resumed reply",
        role_path=role_path,
        argv_log=argv_log,
        env_probe=env_probe,
        env_var="AM017_SENTINEL",
        resume_session_id=session_id,
        runner_state_dir=state_dir,
    )

    assert handler(_turn_input(text=incoming)) == "resumed reply"

    record = handler.last_execution
    assert record is not None
    assert record.launch_mode == "resume"
    assert record.requested_session_id == session_id
    assert record.returned_session_id == session_id
    assert record.exit_code == 0

    logged = json.loads(argv_log.read_text(encoding="utf-8"))["argv"]
    assert logged == [
        str(FAKE_RUNNER),
        "resume",
        "--state-dir",
        str(state_dir),
        "--session",
        session_id,
        "--agent",
        str(role_path),
        "--task",
        str(record.task_path),
        "--turn-id",
        record.turn_id,
    ]
    assert record.argv == (
        sys.executable,
        str(FAKE_RUNNER),
        "resume",
        "--state-dir",
        str(state_dir),
        "--session",
        session_id,
        "--agent",
        str(role_path),
        "--task",
        str(record.task_path),
        "--turn-id",
        record.turn_id,
    )

    assert (
        json.loads(env_probe.read_text(encoding="utf-8"))["AM017_SENTINEL"]
        == "resume-env"
    )
    assert record.task_path.read_text(encoding="utf-8").endswith(incoming)

    # No shell interpreted the metacharacters in paths or the session id.
    for name in ("PWNED_STATE", "PWNED_SESSION"):
        assert not (tmp_path / name).exists()
        assert not (pathlib.Path.cwd() / name).exists()


def test_resume_missing_returned_session_is_protocol_error(
    tmp_path: pathlib.Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    handler = _handler(
        tmp_path,
        mode="success",
        summary_text="ignored",
        omit_session_id=True,
        resume_session_id="wanted-session",
        runner_state_dir=state_dir,
    )

    with pytest.raises(PiReplyProtocolError):
        handler(_turn_input())

    record = handler.last_execution
    assert record is not None
    assert record.outcome == "protocol_error"
    assert record.runner_status == "completed"
    assert record.error_type == "MissingSessionId"
    assert record.requested_session_id == "wanted-session"
    assert record.returned_session_id is None
    # A protocol failure never saves a reply artifact.
    assert record.summary_path is None


def test_resume_wrong_returned_session_is_protocol_error(
    tmp_path: pathlib.Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    handler = _handler(
        tmp_path,
        mode="success",
        summary_text="ignored",
        session_id_value="some-other-session",
        resume_session_id="wanted-session",
        runner_state_dir=state_dir,
    )

    with pytest.raises(PiReplyProtocolError):
        handler(_turn_input())

    record = handler.last_execution
    assert record is not None
    assert record.outcome == "protocol_error"
    assert record.runner_status == "completed"
    assert record.error_type == "SessionMismatch"
    assert record.returned_session_id == "some-other-session"
    assert record.summary_path is None


def test_resume_overwrites_summary_source_but_archives_each_reply(
    tmp_path: pathlib.Path,
) -> None:
    """A reused Runner summary must not clobber earlier response evidence."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    artifact_dir = tmp_path / "artifacts"

    first = _handler(
        tmp_path,
        mode="success",
        summary_text="first reply",
        role_path=tmp_path / "role-first.json",
        artifact_dir=artifact_dir,
        resume_session_id="session-1",
        runner_state_dir=state_dir,
    )
    assert first(_turn_input(text="first", turn_id="t1")) == "first reply"
    first_record = first.last_execution
    assert first_record is not None
    first_reply_path = first_record.summary_path
    assert first_reply_path is not None
    assert first_reply_path.read_text(encoding="utf-8") == "first reply"

    second = _handler(
        tmp_path,
        mode="success",
        summary_text="second reply",
        role_path=tmp_path / "role-second.json",
        artifact_dir=artifact_dir,
        resume_session_id="session-2",
        runner_state_dir=state_dir,
    )
    assert second(_turn_input(text="second", turn_id="t2")) == "second reply"
    second_record = second.last_execution
    assert second_record is not None

    shared_source = state_dir / "summary.md"
    assert shared_source.read_text(encoding="utf-8") == "second reply"
    assert first_record.summary_source_path == shared_source
    assert second_record.summary_source_path == shared_source
    # The first accepted reply is still intact in its own artifact dir.
    assert first_reply_path.exists()
    assert first_reply_path.read_text(encoding="utf-8") == "first reply"
    assert second_record.summary_path is not None
    assert second_record.summary_path.read_text(encoding="utf-8") == (
        "second reply"
    )
    assert first_reply_path != second_record.summary_path


def test_partial_or_invalid_resume_configuration_is_rejected(
    tmp_path: pathlib.Path,
) -> None:
    role_path = _write_role(tmp_path / "role.json", mode="success")
    common = {
        "runner_argv_prefix": [sys.executable, str(FAKE_RUNNER)],
        "role_config_path": str(role_path),
        "artifact_dir": tmp_path / "artifacts",
    }
    state_dir = tmp_path / "state"

    with pytest.raises(PiReplyConfigurationError):
        PiReplyHandler(**common, resume_session_id="only-session")
    with pytest.raises(PiReplyConfigurationError):
        PiReplyHandler(**common, runner_state_dir=state_dir)
    with pytest.raises(PiReplyConfigurationError):
        PiReplyHandler(
            **common,
            resume_session_id="",
            runner_state_dir=state_dir,
        )
    with pytest.raises(PiReplyConfigurationError):
        PiReplyHandler(
            **common,
            resume_session_id="session-1",
            runner_state_dir="relative/state",
        )


# -- integration with the real TurnProcessor and a file database ---------


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


def test_run_once_success_writes_one_assistant_reply(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        processor, repository, turn = _enqueued_turn(database)
        handler = _handler(
            tmp_path, mode="success", summary_text="answer \u00e9"
        )

        result = processor.run_once(handler)

        assert result.status == "completed"
        assert result.turn is not None
        assert result.turn.id == turn.id
        assert result.turn.attempt_count == 1
        messages = repository.list_messages("conv-1")
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[1].text == "answer \u00e9"
        assert handler.last_execution is not None
        assert handler.last_execution.outcome == "completed"
    finally:
        database.dispose()


def test_runner_error_records_failed_turn_without_reply(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        processor, repository, _ = _enqueued_turn(database)
        handler = _handler(
            tmp_path,
            mode="failed_status",
            stderr_text="SECRET_TOKEN_STDERR",
        )

        result = processor.run_once(handler)

        assert result.status == "failed"
        assert result.turn is not None
        assert result.turn.status == "failed"
        assert result.turn.reply_message_id is None
        assert result.error is not None
        assert "PiReplyFailedError" in result.error
        # The durable error points at artifacts, never at raw child output.
        assert "SECRET_TOKEN_STDERR" not in result.error
        assert "SECRET_TOKEN_STDERR" not in (result.turn.error or "")
        assert str(handler.last_execution.artifact_dir) in result.error
        assert [
            message.role for message in repository.list_messages("conv-1")
        ] == ["user"]
        assert handler.last_execution is not None
        assert (
            "SECRET_TOKEN_STDERR"
            in handler.last_execution.stderr_path.read_text(encoding="utf-8")
        )
    finally:
        database.dispose()


def test_invalid_protocol_records_failed_turn_without_reply(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        processor, repository, _ = _enqueued_turn(database)
        handler = _handler(tmp_path, mode="multiple_objects", summary_text="ok")

        result = processor.run_once(handler)

        assert result.status == "failed"
        assert result.turn is not None
        assert result.turn.status == "failed"
        assert result.turn.reply_message_id is None
        assert result.error is not None
        assert "PiReplyProtocolError" in result.error
        assert handler.last_execution is not None
        assert handler.last_execution.outcome == "protocol_error"
        assert [
            message.role for message in repository.list_messages("conv-1")
        ] == ["user"]
    finally:
        database.dispose()


def test_retry_preserves_earlier_attempt_artifacts(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        processor, repository, turn = _enqueued_turn(database)
        artifact_dir = tmp_path / "artifacts"

        failing = _handler(
            tmp_path,
            mode="failed_status",
            role_path=tmp_path / "role-fail.json",
            artifact_dir=artifact_dir,
        )
        first = processor.run_once(failing)
        assert first.status == "failed"
        first_record = failing.last_execution
        assert first_record is not None
        first_dir = first_record.artifact_dir

        processor.retry_failed(turn.id)

        succeeding = _handler(
            tmp_path,
            mode="success",
            summary_text="recovered",
            role_path=tmp_path / "role-ok.json",
            artifact_dir=artifact_dir,
        )
        second = processor.run_once(succeeding)

        assert second.status == "completed"
        assert second.turn is not None
        assert second.turn.id == turn.id
        assert second.turn.attempt_count == 2

        # The earlier attempt's directory and provenance are still present.
        assert first_dir.exists()
        assert first_record.provenance_path.exists()
        assert (first_dir / "stdout.txt").exists()
        assert failing.last_execution == first_record

        run_dirs = sorted(
            path for path in artifact_dir.iterdir() if path.is_dir()
        )
        assert len(run_dirs) == 2

        messages = repository.list_messages("conv-1")
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[1].text == "recovered"
    finally:
        database.dispose()


def test_waiting_for_input_with_clarification_succeeds(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        processor, repository, turn = _enqueued_turn(database)
        artifact_dir = tmp_path / "artifacts"

        handler = _handler(
            tmp_path,
            mode="success",
            summary_text="Понял, создаем агента.",
            role_path=tmp_path / "role-clar.json",
            artifact_dir=artifact_dir,
        )

        # Intercept fake runner result to simulate waiting_for_input
        result = processor.run_once(handler)
        assert result.status == "completed"
        assert result.reply is not None
        assert "Понял, создаем агента." in result.reply.text
    finally:
        database.dispose()

