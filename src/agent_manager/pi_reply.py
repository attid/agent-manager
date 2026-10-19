"""Adapter that exposes the existing Pi Runner as a secretary reply handler.

This module is a thin boundary: it turns one
:class:`~agent_manager.secretary.TurnInput` into one child-process invocation
of the already-existing Pi Runner and returns the Runner's summary text. It
does not import ``pi_runner``, does not change the Runner, adds no HTTP
endpoint and needs no SDK or dependency change.

Context model
-------------

Only the current message is sent. By default every call is a fresh LLM
session: there is no conversation-history loader, compaction, delivery outbox
or automatic retry. A caller may opt in to an explicit resumable session by
configuring ``resume_session_id`` and ``runner_state_dir`` together, but the
adapter never resumes on its own after a failure. A failed call leaves the
turn ``failed`` and an operator must use the existing explicit
``retry_failed`` operation, so a retry cannot silently duplicate side effects.

Child-process contract
----------------------

The Runner command is built as literal ``argv`` entries (never
``shell=True``)::

    <runner_argv_prefix> run --agent <role_config_path> --task <task_file>

With an explicit resumable session the handler is constructed with both
``resume_session_id`` and ``runner_state_dir`` and the command becomes::

    <runner_argv_prefix> resume --state-dir <state_dir> \
        --session <resume_session_id> --agent <role_config_path> \
        --task <task_file>

Every path and identifier is a literal argv entry, so spaces and shell
metacharacters are never interpreted. ``role_config_path`` and
``runner_state_dir`` must be absolute. Providing only one of
``resume_session_id``/``runner_state_dir`` is a construction-time
:class:`PiReplyConfigurationError`, so a handler can never silently fall back
to a fresh session.

``runner_argv_prefix`` is a trusted argument vector, for example the host
Python plus the Runner module and its ``PYTHONPATH``. The child inherits the
caller's environment by default, so the Runner keeps its existing credential
mechanism; the adapter never reads, copies or dumps credentials.

The Runner must print exactly one JSON object to stdout, with no leading log
lines, trailing text or additional objects. A successful fresh result looks
like::

    {"status": "completed", "summary_path": "/abs/path/summary.txt"}

A resumable result must additionally identify the requested session::

    {"status": "completed", "summary_path": "...", "session_id": "<id>"}

Summary text is returned only when all of the following hold:

* the child exited with code ``0``;
* the final stdout JSON object has ``status == "completed"``;
* in resume mode, ``session_id`` is present and exactly equals the requested
  ``resume_session_id`` (missing or different is a protocol error, never a
  success);
* ``summary_path`` (absolute, or relative to the run artifact directory)
  points at readable, nonblank text.

In fresh mode a result without ``session_id`` stays compatible; a later
conversation wrapper may require one before binding. Anything else raises a
typed :class:`PiReplyError`. The error message points at the artifact
directory and never includes raw stdout/stderr or credentials.

Artifacts and provenance
------------------------

Every call creates a unique durable subdirectory under the configured
artifact directory, named from a UTC timestamp and a random id, never from
user input. Earlier attempts are never overwritten. Each run directory holds:

``task.txt``
    The text passed via ``--task``: the turn/conversation/input-message ids,
    the attempt count and role, then the exact incoming message text.
``stdout.txt`` / ``stderr.txt``
    Raw child output, decoded as UTF-8 with replacement.
``result.json``
    The parsed final JSON object, written when stdout parsed.
``reply.md``
    Immutable per-call copy of the accepted reply text. The Runner may reuse
    one summary file across resumed turns; saving the accepted text here means
    a later turn can never overwrite earlier response evidence.
``provenance.json``
    The result record: run id, turn id, conversation id, input message id,
    attempt count, ``argv``, launch mode, requested and returned session ids,
    timestamps, exit code, runner status, outcome, the original summary source
    path, the saved reply path, every other artifact path and the typed error
    name.

The same record is available in memory as
:attr:`PiReplyHandler.last_execution` after every call (success or failure),
so the ``Handler`` -> ``str`` contract is unchanged while the last execution
stays inspectable.

A handler built by
:func:`agent_manager.profile_runtime.freeze_profile_handler` also records the
frozen profile identity, revision, snapshot path and generated role-config
path in every provenance record. Plain handlers leave those optional fields
``None``.

Host invocation sketch
----------------------

.. code-block:: python

    import os

    from agent_manager.pi_reply import PiReplyHandler
    from agent_manager.secretary import TurnProcessor
    from agent_manager.storage import Database

    handler = PiReplyHandler(
        runner_argv_prefix=[
            "/path/to/host/python",
            "-m",
            "pi_runner",
        ],
        role_config_path="/abs/path/role.json",
        artifact_dir="/var/lib/agent-manager/pi-runs",
        # env defaults to None: the child inherits os.environ unchanged.
        env={**os.environ, "PYTHONPATH": "/abs/path/to/host/src"},
    )

    processor = TurnProcessor(Database.from_path("/abs/path/agent_manager.db"))
    result = processor.run_once(handler)
    if result.status == "failed":
        print(handler.last_execution.provenance_path)
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .secretary import TurnInput

__all__ = [
    "PiReplyCommandError",
    "PiReplyConfigurationError",
    "PiReplyError",
    "PiReplyFailedError",
    "PiReplyHandler",
    "PiReplyLaunchError",
    "PiReplyProtocolError",
    "PiReplySummaryError",
    "PiRunProvenance",
]

#: Provenance ``outcome`` values written on every run.
OUTCOME_COMPLETED = "completed"
OUTCOME_LAUNCH_ERROR = "launch_error"
OUTCOME_COMMAND_ERROR = "command_error"
OUTCOME_PROTOCOL_ERROR = "protocol_error"
OUTCOME_RUNNER_FAILED = "runner_failed"
OUTCOME_SUMMARY_UNAVAILABLE = "summary_unavailable"

#: Launch modes recorded in provenance and used as the Runner subcommand.
LAUNCH_MODE_RUN = "run"
LAUNCH_MODE_RESUME = "resume"

#: Immutable per-call copy of the accepted reply text.
SAVED_REPLY_FILENAME = "reply.md"


class PiReplyConfigurationError(ValueError):
    """The trusted handler configuration cannot form a valid argv.

    Raised at construction time, for example when only one of
    ``resume_session_id``/``runner_state_dir`` is supplied. It subclasses
    :class:`ValueError` so existing callers that validate handler arguments
    keep working.
    """


class PiReplyError(Exception):
    """Base class for the adapter's typed failures.

    The message points at the artifact directory for diagnosis. It never
    contains raw stdout/stderr or credentials; those live only in the run's
    artifact files.
    """

    def __init__(
        self,
        message: str,
        *,
        artifact_dir: pathlib.Path,
        turn_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.artifact_dir = pathlib.Path(artifact_dir)
        self.turn_id = turn_id


class PiReplyLaunchError(PiReplyError):
    """The child process could not be started at all."""


class PiReplyCommandError(PiReplyError):
    """The child process exited with a non-zero status."""

    def __init__(
        self,
        *,
        artifact_dir: pathlib.Path,
        exit_code: int,
        turn_id: str | None = None,
    ) -> None:
        super().__init__(
            f"pi runner exited with code {exit_code}; "
            f"see artifacts in {artifact_dir}",
            artifact_dir=artifact_dir,
            turn_id=turn_id,
        )
        self.exit_code = exit_code


class PiReplyProtocolError(PiReplyError):
    """Stdout was not exactly one JSON object with a status."""


class PiReplyFailedError(PiReplyError):
    """The child exited 0 but the runner reported a non-completed status."""

    def __init__(
        self,
        *,
        artifact_dir: pathlib.Path,
        runner_status: str,
        turn_id: str | None = None,
    ) -> None:
        super().__init__(
            f"pi runner reported status {runner_status!r}; "
            f"see artifacts in {artifact_dir}",
            artifact_dir=artifact_dir,
            turn_id=turn_id,
        )
        self.runner_status = runner_status


class PiReplySummaryError(PiReplyError):
    """A completed result had no readable, nonblank summary."""


@dataclasses.dataclass(frozen=True)
class PiRunProvenance:
    """In-memory result record for one adapter call (also written to disk)."""

    run_id: str
    turn_id: str
    conversation_id: str
    input_message_id: str
    attempt_count: int
    artifact_dir: pathlib.Path
    task_path: pathlib.Path
    stdout_path: pathlib.Path
    stderr_path: pathlib.Path
    provenance_path: pathlib.Path
    result_path: pathlib.Path | None
    summary_path: pathlib.Path | None
    argv: tuple[str, ...]
    started_at: str
    finished_at: str
    exit_code: int | None
    runner_status: str | None
    outcome: str
    launch_mode: str
    error_type: str | None
    error_message: str | None
    requested_session_id: str | None = None
    returned_session_id: str | None = None
    summary_source_path: pathlib.Path | None = None
    role_config_path: pathlib.Path | None = None
    profile_agent_id: str | None = None
    profile_revision: int | None = None
    profile_snapshot_path: pathlib.Path | None = None
    runtime_defaults: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready provenance mapping written to disk."""
        document = {
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "input_message_id": self.input_message_id,
            "attempt_count": self.attempt_count,
            "artifact_dir": str(self.artifact_dir),
            "files": {
                "task": str(self.task_path),
                "stdout": str(self.stdout_path),
                "stderr": str(self.stderr_path),
                "provenance": str(self.provenance_path),
                "result": (
                    str(self.result_path) if self.result_path is not None else None
                ),
                "summary": (
                    str(self.summary_path)
                    if self.summary_path is not None
                    else None
                ),
                "summary_source": (
                    str(self.summary_source_path)
                    if self.summary_source_path is not None
                    else None
                ),
            },
            "argv": list(self.argv),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "runner_status": self.runner_status,
            "outcome": self.outcome,
            "launch_mode": self.launch_mode,
            "requested_session_id": self.requested_session_id,
            "returned_session_id": self.returned_session_id,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "role_config_path": (
                str(self.role_config_path)
                if self.role_config_path is not None
                else None
            ),
            "profile_agent_id": self.profile_agent_id,
            "profile_revision": self.profile_revision,
            "profile_snapshot_path": (
                str(self.profile_snapshot_path)
                if self.profile_snapshot_path is not None
                else None
            ),
        }
        if self.runtime_defaults is not None:
            document["runtime_defaults"] = self.runtime_defaults
        return document


class PiReplyHandler:
    """Callable ``Handler`` that runs the existing Pi Runner as a child.

    Instances are configured once with a trusted argv prefix, an absolute role
    config path and an artifact directory. Each call runs one child process,
    writes one durable artifact directory and returns the summary text. There
    is no internal retry. When both ``resume_session_id`` and
    ``runner_state_dir`` are supplied the Runner is invoked in ``resume`` mode;
    otherwise every call is a fresh ``run``.
    """

    def __init__(
        self,
        *,
        runner_argv_prefix: Sequence[str],
        role_config_path: str | os.PathLike[str],
        artifact_dir: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
        profile_agent_id: str | None = None,
        profile_revision: int | None = None,
        profile_snapshot_path: str | os.PathLike[str] | None = None,
        resume_session_id: str | None = None,
        runner_state_dir: str | os.PathLike[str] | None = None,
        runtime_defaults_provenance: Mapping[str, Any] | None = None,
    ) -> None:
        prefix = tuple(runner_argv_prefix)
        if not prefix or any(
            not isinstance(item, str) or not item for item in prefix
        ):
            raise ValueError(
                "runner_argv_prefix must be a non-empty sequence of "
                "non-empty strings"
            )
        role = pathlib.Path(role_config_path)
        if not role.is_absolute():
            raise ValueError("role_config_path must be an absolute path")

        # Resume is explicit and all-or-nothing: both a session id and a
        # state directory are required, or the handler always starts fresh.
        if (resume_session_id is None) != (runner_state_dir is None):
            raise PiReplyConfigurationError(
                "resume_session_id and runner_state_dir must be provided "
                "together"
            )
        if resume_session_id is not None:
            if not isinstance(resume_session_id, str) or not resume_session_id:
                raise PiReplyConfigurationError(
                    "resume_session_id must be a non-empty string"
                )
            state_dir = pathlib.Path(runner_state_dir)
            if not state_dir.is_absolute():
                raise PiReplyConfigurationError(
                    "runner_state_dir must be an absolute path"
                )
        else:
            state_dir = None

        self._runner_argv_prefix = prefix
        self._role_config_path = role
        self._artifact_dir = pathlib.Path(artifact_dir).absolute()
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._launch_mode = (
            LAUNCH_MODE_RESUME
            if resume_session_id is not None
            else LAUNCH_MODE_RUN
        )
        self._resume_session_id = resume_session_id
        self._runner_state_dir = state_dir
        # ``env=None`` inherits the caller's process environment unchanged, so
        # the Runner keeps its existing credential mechanism.
        self._env = dict(env) if env is not None else None
        # Optional frozen-profile context. It is provenance metadata only;
        # the handler still reads all runtime parameters from its configured
        # role file and never queries the registry itself.
        self._profile_agent_id = profile_agent_id
        self._profile_revision = profile_revision
        self._profile_snapshot_path = (
            pathlib.Path(profile_snapshot_path)
            if profile_snapshot_path is not None
            else None
        )
        # Optional runtime-default evidence captured by the frozen-profile
        # factory. It is provenance metadata only: the adapter still reads all
        # runtime parameters from its configured role file. ``None`` keeps the
        # legacy provenance shape unchanged.
        self._runtime_defaults_provenance = (
            dict(runtime_defaults_provenance)
            if runtime_defaults_provenance is not None
            else None
        )
        self._last_execution: PiRunProvenance | None = None

    @property
    def last_execution(self) -> PiRunProvenance | None:
        """Result record of the most recent call, success or failure."""
        return self._last_execution

    def __call__(self, turn_input: TurnInput) -> str:
        """Run the Runner once for ``turn_input`` and return summary text.

        Raises a typed :class:`PiReplyError` when the child fails, emits no
        final JSON object, reports a non-completed status, or leaves no
        readable nonblank summary.
        """
        run_id = uuid.uuid4().hex
        artifact_dir = self._artifact_dir / f"{_compact_utc()}-{run_id}"
        artifact_dir.mkdir(parents=True, exist_ok=False)

        task_path = artifact_dir / "task.txt"
        stdout_path = artifact_dir / "stdout.txt"
        stderr_path = artifact_dir / "stderr.txt"
        result_path = artifact_dir / "result.json"
        provenance_path = artifact_dir / "provenance.json"

        _write_text(task_path, _render_task_text(turn_input))

        argv = self._build_argv(task_path)
        started_at = _utcnow_iso()

        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                env=self._env,
                # No shell: every path is a literal argv entry, so no word
                # splitting, globbing or command substitution can occur.
                check=False,
            )
        except OSError as error:
            self._record(
                run_id=run_id,
                turn_input=turn_input,
                artifact_dir=artifact_dir,
                task_path=task_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                provenance_path=provenance_path,
                result_path=None,
                summary_path=None,
                argv=argv,
                started_at=started_at,
                exit_code=None,
                runner_status=None,
                outcome=OUTCOME_LAUNCH_ERROR,
                error_type=type(error).__name__,
            )
            raise PiReplyLaunchError(
                f"could not launch pi runner ({type(error).__name__}); "
                f"see artifacts in {artifact_dir}",
                artifact_dir=artifact_dir,
                turn_id=turn_input.turn_id,
            ) from error

        stdout_text = _decode(completed.stdout)
        stderr_text = _decode(completed.stderr)
        _write_text(stdout_path, stdout_text)
        _write_text(stderr_path, stderr_text)

        if completed.returncode != 0:
            self._record(
                run_id=run_id,
                turn_input=turn_input,
                artifact_dir=artifact_dir,
                task_path=task_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                provenance_path=provenance_path,
                result_path=None,
                summary_path=None,
                argv=argv,
                started_at=started_at,
                exit_code=completed.returncode,
                runner_status=None,
                outcome=OUTCOME_COMMAND_ERROR,
            )
            raise PiReplyCommandError(
                artifact_dir=artifact_dir,
                exit_code=completed.returncode,
                turn_id=turn_input.turn_id,
            )

        try:
            result = _extract_result(stdout_text)
        except ValueError as error:
            self._record(
                run_id=run_id,
                turn_input=turn_input,
                artifact_dir=artifact_dir,
                task_path=task_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                provenance_path=provenance_path,
                result_path=None,
                summary_path=None,
                argv=argv,
                started_at=started_at,
                exit_code=completed.returncode,
                runner_status=None,
                outcome=OUTCOME_PROTOCOL_ERROR,
                error_type=type(error).__name__,
            )
            raise PiReplyProtocolError(
                "pi runner did not emit exactly one JSON object; "
                f"see artifacts in {artifact_dir}",
                artifact_dir=artifact_dir,
                turn_id=turn_input.turn_id,
            ) from error

        _write_json(result_path, result)
        returned_session_id = _session_id_or_none(result.get("session_id"))

        runner_status = result.get("status")
        if not isinstance(runner_status, str) or not runner_status:
            self._record(
                run_id=run_id,
                turn_input=turn_input,
                artifact_dir=artifact_dir,
                task_path=task_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                provenance_path=provenance_path,
                result_path=result_path,
                summary_path=None,
                argv=argv,
                started_at=started_at,
                exit_code=completed.returncode,
                runner_status=None,
                outcome=OUTCOME_PROTOCOL_ERROR,
                error_type="MissingStatus",
                returned_session_id=returned_session_id,
            )
            raise PiReplyProtocolError(
                "pi runner result had no status string; "
                f"see artifacts in {artifact_dir}",
                artifact_dir=artifact_dir,
                turn_id=turn_input.turn_id,
            )

        if runner_status not in ("completed", "waiting_for_input"):
            self._record(
                run_id=run_id,
                turn_input=turn_input,
                artifact_dir=artifact_dir,
                task_path=task_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                provenance_path=provenance_path,
                result_path=result_path,
                summary_path=None,
                argv=argv,
                started_at=started_at,
                exit_code=completed.returncode,
                runner_status=runner_status,
                outcome=OUTCOME_RUNNER_FAILED,
                error_type="RunnerStatus",
                returned_session_id=returned_session_id,
            )
            raise PiReplyFailedError(
                artifact_dir=artifact_dir,
                runner_status=runner_status,
                turn_id=turn_input.turn_id,
            )

        # A resume call is only successful when the returned session is the
        # one that was requested. Missing or different ids are protocol
        # errors, never recovered into a success.
        if (
            self._launch_mode == LAUNCH_MODE_RESUME
            and returned_session_id != self._resume_session_id
        ):
            self._record(
                run_id=run_id,
                turn_input=turn_input,
                artifact_dir=artifact_dir,
                task_path=task_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                provenance_path=provenance_path,
                result_path=result_path,
                summary_path=None,
                argv=argv,
                started_at=started_at,
                exit_code=completed.returncode,
                runner_status=runner_status,
                outcome=OUTCOME_PROTOCOL_ERROR,
                error_type=(
                    "MissingSessionId"
                    if returned_session_id is None
                    else "SessionMismatch"
                ),
                returned_session_id=returned_session_id,
            )
            raise PiReplyProtocolError(
                "pi runner resume result did not identify the requested "
                f"session; see artifacts in {artifact_dir}",
                artifact_dir=artifact_dir,
                turn_id=turn_input.turn_id,
            )

        raw_summary = result.get("summary_path")
        summary_text = ""
        summary_source_path = None
        if isinstance(raw_summary, str) and raw_summary.strip():
            summary_source_path = _resolve_summary_path(raw_summary, artifact_dir)
            try:
                summary_text = summary_source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                summary_text = ""

        # If summary is missing or empty, but clarification is present, extract question/description
        if not summary_text.strip() and result.get("clarification"):
            clar = result["clarification"]
            if isinstance(clar, dict):
                parts = []
                if clar.get("title"):
                    parts.append(f"**{clar['title']}**")
                if clar.get("description"):
                    parts.append(clar["description"])
                if clar.get("question"):
                    parts.append(f"❓ {clar['question']}")
                summary_text = "\n\n".join(parts)

        if not summary_text.strip():
            self._record(
                run_id=run_id,
                turn_input=turn_input,
                artifact_dir=artifact_dir,
                task_path=task_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                provenance_path=provenance_path,
                result_path=result_path,
                summary_path=None,
                summary_source_path=summary_source_path,
                argv=argv,
                started_at=started_at,
                exit_code=completed.returncode,
                runner_status=runner_status,
                outcome=OUTCOME_SUMMARY_UNAVAILABLE,
                error_type="EmptySummary",
                returned_session_id=returned_session_id,
            )
            raise PiReplySummaryError(
                "pi runner summary is empty; "
                f"see artifacts in {artifact_dir}",
                artifact_dir=artifact_dir,
                turn_id=turn_input.turn_id,
            )

        # Save the accepted reply as an immutable per-call artifact. The
        # Runner may reuse one summary source file across resumed turns, so
        # this copy is what keeps earlier response evidence intact.
        saved_summary_path = artifact_dir / SAVED_REPLY_FILENAME
        _write_text(saved_summary_path, summary_text)

        self._record(
            run_id=run_id,
            turn_input=turn_input,
            artifact_dir=artifact_dir,
            task_path=task_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            provenance_path=provenance_path,
            result_path=result_path,
            summary_path=saved_summary_path,
            summary_source_path=summary_source_path,
            argv=argv,
            started_at=started_at,
            exit_code=completed.returncode,
            runner_status=runner_status,
            outcome=OUTCOME_COMPLETED,
            returned_session_id=returned_session_id,
        )
        return summary_text

    def _record(
        self,
        *,
        run_id: str,
        turn_input: TurnInput,
        artifact_dir: pathlib.Path,
        task_path: pathlib.Path,
        stdout_path: pathlib.Path,
        stderr_path: pathlib.Path,
        provenance_path: pathlib.Path,
        result_path: pathlib.Path | None,
        summary_path: pathlib.Path | None,
        argv: tuple[str, ...],
        started_at: str,
        exit_code: int | None,
        runner_status: str | None,
        outcome: str,
        error_type: str | None = None,
        error_message: str | None = None,
        summary_source_path: pathlib.Path | None = None,
        returned_session_id: str | None = None,
    ) -> PiRunProvenance:
        """Persist one run record and remember it as the last execution."""
        record = PiRunProvenance(
            run_id=run_id,
            turn_id=turn_input.turn_id,
            conversation_id=turn_input.conversation_id,
            input_message_id=turn_input.input_message_id,
            attempt_count=turn_input.attempt_count,
            artifact_dir=artifact_dir,
            task_path=task_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            provenance_path=provenance_path,
            result_path=result_path,
            summary_path=summary_path,
            argv=tuple(argv),
            started_at=started_at,
            finished_at=_utcnow_iso(),
            exit_code=exit_code,
            runner_status=runner_status,
            outcome=outcome,
            launch_mode=self._launch_mode,
            error_type=error_type,
            error_message=error_message,
            requested_session_id=self._resume_session_id,
            returned_session_id=returned_session_id,
            summary_source_path=summary_source_path,
            role_config_path=self._role_config_path,
            profile_agent_id=self._profile_agent_id,
            profile_revision=self._profile_revision,
            profile_snapshot_path=self._profile_snapshot_path,
            runtime_defaults=self._runtime_defaults_provenance,
        )
        _write_json(provenance_path, record.to_dict())
        self._last_execution = record
        return record

    def _build_argv(self, task_path: pathlib.Path) -> tuple[str, ...]:
        """Return the literal Runner argv for this handler's launch mode.

        Resume mode emits ``resume --state-dir <dir> --session <id>`` before
        the always-present ``--agent``/``--task`` pair; fresh mode emits
        ``run``. Every value is a separate argv entry, so no shell ever sees
        spaces or metacharacters.
        """
        if self._launch_mode == LAUNCH_MODE_RESUME:
            return (
                *self._runner_argv_prefix,
                LAUNCH_MODE_RESUME,
                "--state-dir",
                str(self._runner_state_dir),
                "--session",
                str(self._resume_session_id),
                "--agent",
                str(self._role_config_path),
                "--task",
                str(task_path),
            )
        return (
            *self._runner_argv_prefix,
            LAUNCH_MODE_RUN,
            "--agent",
            str(self._role_config_path),
            "--task",
            str(task_path),
        )


def _session_id_or_none(value: Any) -> str | None:
    """Return a non-empty string session id, else ``None``.

    No identifier format is imposed; the id only has to be usable as an argv
    entry and comparable to the requested session.
    """
    if isinstance(value, str) and value:
        return value
    return None


def _render_task_text(turn_input: TurnInput) -> str:
    """Return the exact text passed to the Runner via ``--task``.

    The header records the turn/conversation/input-message ids and attempt
    count; the incoming message text follows the blank line verbatim. No size
    limit is imposed.
    """
    header = (
        f"turn_id: {turn_input.turn_id}\n"
        f"conversation_id: {turn_input.conversation_id}\n"
        f"input_message_id: {turn_input.input_message_id}\n"
        f"attempt_count: {turn_input.attempt_count}\n"
        f"role: {turn_input.role}\n"
    )
    return f"{header}\n{turn_input.text}"


def _extract_result(stdout_text: str) -> dict[str, Any]:
    """Return the single JSON object printed on stdout.

    The Runner CLI protocol is exactly one JSON object on stdout. Whitespace
    and pretty-printed JSON are valid; leading log lines, trailing garbage or
    multiple objects are protocol errors. The whole stripped stdout is parsed
    and must be one JSON object. Raises ``ValueError`` otherwise.
    """
    stripped = stdout_text.strip()
    if not stripped:
        raise ValueError("stdout was empty")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise ValueError("stdout was not one JSON object") from error
    if not isinstance(parsed, dict):
        raise ValueError("stdout JSON was not an object")
    return parsed


def _resolve_summary_path(raw: str, artifact_dir: pathlib.Path) -> pathlib.Path:
    """Resolve ``summary_path``: absolute as given, relative to the run dir."""
    candidate = pathlib.Path(raw)
    if candidate.is_absolute():
        return candidate
    return artifact_dir / candidate


def _decode(data: bytes | None) -> str:
    """Decode captured child output without failing on stray bytes."""
    return (data or b"").decode("utf-8", errors="replace")


def _compact_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _write_text(path: pathlib.Path, text: str) -> None:
    """Write one artifact durably (UTF-8, flushed and fsynced)."""
    data = text.encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: pathlib.Path, value: Any) -> None:
    _write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
