"""Deterministic fake Pi Runner for adapter acceptance tests.

This is a real child Python process driven only by the command line and a role
configuration JSON file. It never calls a model or the network. It emulates
the existing Runner interface::

    <this script> run --agent ROLE_JSON --task TASK_FILE
    <this script> resume --state-dir STATE_DIR --session SESSION_ID \
        --agent ROLE_JSON --task TASK_FILE

The role JSON is a plain local test control file. Recognised fields:

``mode``
    ``success`` (default), ``relative_summary``, ``failed_status``,
    ``nonzero``, ``malformed``, ``trailing_junk``, ``multiple_objects``,
    ``missing_summary``, ``empty_summary`` or ``no_status``.
``summary_text``
    Text written to the summary file. Defaults to ``"fake summary"``.
``write_files``
    Optional mapping of relative path to text content. The fake Runner
    writes each file relative to its working directory before emitting the
    result, so an acceptance test can make the deterministic "developer"
    produce a real working-tree edit instead of inserting a database row.
``shared_summary_path``
    Optional absolute summary file to write. A ``resume`` call with no such
    override writes ``<state-dir>/summary.md`` so successive resumable turns
    reuse one summary source; a ``run`` call writes ``<task-dir>/summary.txt``.
``session_id_value``
    Optional ``session_id`` value echoed in the result JSON. When unset, a
    ``resume`` call echoes the requested session; any other string exercises a
    mismatched session.
``omit_session_id``
    When true, the result JSON omits ``session_id`` even for ``resume``, which
    lets tests exercise a missing returned session id.
``log_line``
    Optional line printed to stdout before the JSON object. The Runner
    protocol does not allow leading logs, so this is used to prove the
    adapter fails closed on them.
``stderr_text``
    Optional text written to stderr.
``argv_log``
    Optional path to write the process argv as JSON.
``instructions_probe``
    Optional path to write the resolved ``instructions`` entries as JSON. A
    string entry that names an existing file is recorded with its path and
    full text; other entries are recorded as values. This lets tests prove
    which prompt/instruction files actually reached the child process.
``env_probe`` / ``env_var``
    Optional path and variable name; the child writes the variable's value
    from its inherited environment, proving environment pass-through.
``selection_probe``
    Optional path to write the role's resolved ``provider``/``model``/
    ``pi_auth_provider``/``models_file`` values as JSON. This proves which
    per-call runtime selection actually reached the child process.
``stateful_session``
    When true, the fake Runner emulates the real Runner's trusted state
    policy: a fresh ``run`` writes the native session under the ``state_dir``
    from the role JSON, and a later ``resume`` only succeeds when its
    ``--state-dir`` resolves to that same directory and the stored session
    matches ``--session``. This makes an invented state path fail loudly.
``state_dir``
    The trusted state directory used by ``stateful_session``.
``media_probe``
    Optional path for a JSON probe of the role's explicit media mounts. It
    records the task text, files in ``inbox_dir``, files in the profile
    ``project/inbox`` fallback, and files in the read-only
    ``attachments_dir`` store. This lets acceptance tests prove which files
    were actually presented as current attachments.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any

_EXIT_USAGE = 64


def _failure(message: str):
    sys.stderr.write(message + "\n")
    raise SystemExit(_EXIT_USAGE)


def _parse_argv(argv: list[str]) -> dict[str, str]:
    if not argv or argv[0] not in ("run", "resume"):
        _failure(
            "usage: fake_pi_runner.py (run|resume) --agent ROLE --task TASK"
        )
    command = argv[0]
    parsed: dict[str, str] = {"command": command}
    allowed = {"--agent", "--task"}
    if command == "resume":
        allowed |= {"--state-dir", "--session"}
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in allowed and index + 1 < len(argv):
            parsed[token.lstrip("-")] = argv[index + 1]
            index += 2
        else:
            _failure(f"unexpected argument: {token!r}")
    if "agent" not in parsed or "task" not in parsed:
        _failure("missing --agent or --task")
    if command == "resume" and (
        "state-dir" not in parsed or "session" not in parsed
    ):
        _failure("resume requires --state-dir and --session")
    return parsed


def _load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        _failure("role config must be a JSON object")
    return loaded


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _list_files(directory: str) -> list[dict[str, Any]]:
    """Return deterministic file facts below ``directory`` for test probes."""
    listed: list[dict[str, Any]] = []
    if not os.path.isdir(directory):
        return listed
    for root, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                content = handle.read()
            listed.append(
                {
                    "relative": os.path.relpath(path, directory),
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes_hex": content.hex(),
                }
            )
    return listed


def _emit(payload: dict[str, Any], log_line: str | None) -> None:
    if log_line:
        print(log_line)
    print(json.dumps(payload, ensure_ascii=False))


def main() -> None:
    parsed = _parse_argv(sys.argv[1:])
    role = _load_json(parsed["agent"])

    argv_log = role.get("argv_log")
    if argv_log:
        _write_text(
            argv_log,
            json.dumps({"argv": sys.argv}, ensure_ascii=False, indent=2),
        )

    instructions_probe = role.get("instructions_probe")
    if instructions_probe:
        raw_instructions = role.get("instructions", [])
        if isinstance(raw_instructions, str):
            entries = [raw_instructions]
        elif isinstance(raw_instructions, list):
            entries = raw_instructions
        else:
            entries = []
        resolved = []
        for entry in entries:
            if isinstance(entry, str) and os.path.isfile(entry):
                with open(entry, encoding="utf-8") as handle:
                    resolved.append(
                        {"path": entry, "text": handle.read()}
                    )
            else:
                resolved.append({"value": entry})
        _write_text(
            instructions_probe,
            json.dumps(resolved, ensure_ascii=False, indent=2),
        )

    selection_probe = role.get("selection_probe")
    if selection_probe:
        _write_text(
            selection_probe,
            json.dumps(
                {
                    "provider": role.get("provider"),
                    "model": role.get("model"),
                    "pi_auth_provider": role.get("pi_auth_provider"),
                    "models_file": role.get("models_file"),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    write_files = role.get("write_files")
    if isinstance(write_files, dict):
        for relative_path, content in write_files.items():
            if not isinstance(relative_path, str) or not relative_path:
                _failure("write_files keys must be non-empty strings")
            destination = os.path.join(os.getcwd(), relative_path)
            destination_parent = os.path.dirname(destination)
            if destination_parent:
                os.makedirs(destination_parent, exist_ok=True)
            _write_text(destination, str(content))

    stderr_text = role.get("stderr_text")
    if stderr_text:
        sys.stderr.write(stderr_text)
        sys.stderr.flush()

    env_probe = role.get("env_probe")
    if env_probe:
        env_var = role.get("env_var", "")
        _write_text(env_probe, json.dumps({env_var: os.environ.get(env_var)}))

    media_probe = role.get("media_probe")
    if media_probe:
        task_text = ""
        try:
            with open(parsed["task"], encoding="utf-8") as handle:
                task_text = handle.read()
        except OSError:
            pass
        inbox_dir = role.get("inbox_dir")
        if not isinstance(inbox_dir, str):
            inbox_dir = ""
        project = role.get("project")
        project_inbox = (
            os.path.join(project, "inbox")
            if isinstance(project, str)
            else ""
        )
        attachments_dir = role.get("attachments_dir")
        if not isinstance(attachments_dir, str):
            attachments_dir = ""
        _write_text(
            media_probe,
            json.dumps(
                {
                    "task_text": task_text,
                    "inbox_dir": inbox_dir,
                    "inbox_files": _list_files(inbox_dir),
                    "project_inbox": project_inbox,
                    "project_inbox_files": _list_files(project_inbox),
                    "attachments_dir": attachments_dir,
                    "attachments_files": _list_files(attachments_dir),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    task_path = parsed["task"]
    run_dir = os.path.dirname(task_path)

    if role.get("stateful_session"):
        role_state_dir = role.get("state_dir")
        if not isinstance(role_state_dir, str) or not role_state_dir:
            _failure("stateful_session requires a state_dir string")
        marker_path = os.path.join(role_state_dir, "native_session.json")
        if parsed["command"] == "run":
            os.makedirs(role_state_dir, exist_ok=True)
            _write_text(
                marker_path,
                json.dumps(
                    {"session_id": role.get("session_id_value")}
                ),
            )
        else:
            state_dir_arg = parsed.get("state-dir", "")
            if os.path.realpath(role_state_dir) != os.path.realpath(
                state_dir_arg
            ):
                _failure(
                    "resume state-dir does not match role state_dir: "
                    f"{state_dir_arg!r} != {role_state_dir!r}"
                )
            if not os.path.isfile(marker_path):
                _failure(
                    f"resume state dir has no native session: "
                    f"{role_state_dir!r}"
                )
            with open(marker_path, encoding="utf-8") as handle:
                stored_session = json.load(handle).get("session_id")
            if stored_session != parsed.get("session"):
                _failure(
                    "resume session does not match stored native session"
                )

    summary_path = role.get("shared_summary_path")
    if not summary_path and parsed["command"] == "resume":
        summary_path = os.path.join(parsed["state-dir"], "summary.md")
    if not summary_path:
        summary_path = os.path.join(run_dir, "summary.txt")
    summary_parent = os.path.dirname(summary_path)
    if summary_parent:
        os.makedirs(summary_parent, exist_ok=True)
    summary_text = role.get("summary_text", "fake summary")
    log_line = role.get("log_line")
    mode = role.get("mode", "success")
    session_payload: dict[str, Any] = {}
    if role.get("omit_session_id"):
        pass
    elif "session_id_value" in role:
        session_payload["session_id"] = role["session_id_value"]
    elif parsed["command"] == "resume":
        # A working resume call echoes the session it was asked to resume.
        session_payload["session_id"] = parsed["session"]
    completed_payload: dict[str, Any] = {
        "status": "completed",
        "summary_path": summary_path,
        "changed_files": role.get("changed_files", []),
        **session_payload,
    }

    if mode == "malformed":
        print("this line is not a JSON object")
        raise SystemExit(0)

    if mode == "trailing_junk":
        _write_text(summary_path, summary_text)
        print(
            json.dumps(completed_payload, ensure_ascii=False) + " trailing junk"
        )
        raise SystemExit(0)

    if mode == "multiple_objects":
        _write_text(summary_path, summary_text)
        print(json.dumps({"status": "failed", **session_payload}))
        print(json.dumps(completed_payload, ensure_ascii=False))
        raise SystemExit(0)

    if mode == "missing_summary":
        _emit(
            {
                "status": "completed",
                "summary_path": os.path.join(run_dir, "absent.txt"),
                **session_payload,
            },
            log_line,
        )
        raise SystemExit(0)

    if mode == "empty_summary":
        _write_text(summary_path, "")
        _emit(completed_payload, log_line)
        raise SystemExit(0)

    if mode == "no_status":
        _emit({"summary_path": summary_path, **session_payload}, log_line)
        raise SystemExit(0)

    if mode == "failed_status":
        _write_text(summary_path, summary_text)
        _emit(
            {"status": "failed", "summary_path": summary_path, **session_payload},
            log_line,
        )
        raise SystemExit(0)

    if mode == "nonzero":
        _write_text(summary_path, summary_text)
        _emit(completed_payload, log_line)
        raise SystemExit(3)

    if mode == "relative_summary":
        _write_text(summary_path, summary_text)
        _emit(
            {"status": "completed", "summary_path": "summary.txt", **session_payload},
            log_line,
        )
        raise SystemExit(0)

    if mode == "success":
        _write_text(summary_path, summary_text)
        _emit(completed_payload, log_line)
        raise SystemExit(0)

    _failure(f"unknown mode: {mode!r}")


if __name__ == "__main__":
    main()
