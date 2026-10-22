"""Live execution monitor for running tasks and durable operations.

The monitor is a read-only projection over two cheap sources:

* durable rows (task execution metadata, operations and queues); and
* the newest Runner ``events.jsonl`` artifact for the execution.

Event files are written incrementally by the Runner. Only the tail is read
and parsed, so a long-lived session never causes the monitor to load a whole
transcript. When no event file is available the execution still appears with
its elapsed time; session counters are reported as unavailable instead of
invented or crashing the endpoint.

``last_activity_sec`` is the age of the newest writing activity, not a
liveness verdict. A model can spend minutes thinking without emitting an
event, so the monitor reports the age and deliberately makes no "stalled"
claim.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from .storage import (
    OPERATION_QUEUED_STATUSES,
    OPERATION_STATUS_RUNNING,
    Database,
    Operation,
    Task,
    Turn,
)
from .storage.models import TaskSubscription

#: Byte and record bounds for one incremental event tail.
EVENTS_TAIL_BYTES = 256 * 1024
EVENTS_TAIL_RECORDS = 2_000
#: Metadata reads are bounded, but path discovery is complete so the active
#: session can never be hidden behind a fixed sample of older history.
MAX_METADATA_HEAD_BYTES = 8 * 1024
MAX_ROLE_CONFIG_BYTES = 1_000_000

#: Human labels used by the API and Telegram rendering.
OPERATION_LABELS = {
    "analysis": "Анализ",
    "review": "Ревью",
    "git.commit_push": "Git commit/push",
    "git.rollback": "Git rollback",
}

OPERATION_AGENTS = {
    "analysis": "system-log-analyzer",
    "review": "system-code-reviewer",
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _aware_utc(value: dt.datetime) -> dt.datetime:
    """Return ``value`` as an aware UTC datetime.

    Timestamps read from SQLite and ``_utcnow`` are naive UTC. Calling
    ``timestamp()`` on a naive datetime uses the host local timezone, which
    makes activity ages wrong outside UTC; attaching UTC first keeps the
    monitor timezone-independent.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _as_naive_utc(value: Any) -> dt.datetime | None:
    """Normalize a stored timestamp (datetime or ISO string) to naive UTC."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return moment


def _elapsed_seconds(value: Any, now: dt.datetime) -> int:
    moment = _as_naive_utc(value)
    if moment is None:
        return 0
    return max(0, int((_aware_utc(now) - _aware_utc(moment)).total_seconds()))


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _usage_from_event(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    usage = event.get("usage")
    if isinstance(usage, Mapping):
        return usage
    message = event.get("message")
    if isinstance(message, Mapping):
        usage = message.get("usage")
        if isinstance(usage, Mapping):
            return usage
    return None


def _usage_snapshot(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Return the counters carried by one ``usage`` object.

    The Runner emits ``cost`` as ``{"total": X}``. Counters are kept as
    numbers; nested cost is flattened to ``cost`` so the API and Telegram
    renderer share one shape.
    """
    result: dict[str, Any] = {}
    for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
        number = _number(usage.get(key))
        if number is not None:
            result[key] = number
    raw_cost = usage.get("cost")
    if isinstance(raw_cost, Mapping):
        raw_cost = raw_cost.get("total")
    cost = _number(raw_cost)
    if cost is not None:
        result["cost"] = cost
    # Preserve any extra numeric counters without turning nested objects into
    # unbounded JSON.
    for key, value in usage.items():
        if key in result or key in {"cost", "input", "output", "cacheRead", "cacheWrite", "totalTokens"}:
            continue
        number = _number(value)
        if number is not None and isinstance(key, str):
            result[key] = number
    return result


def _tail_records(path: Path) -> list[Mapping[str, Any]]:
    """Read at most the last bounded chunk of one JSONL file."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > EVENTS_TAIL_BYTES:
                stream.seek(size - EVENTS_TAIL_BYTES)
                data = stream.read(EVENTS_TAIL_BYTES)
                # The first line may be a byte-boundary fragment.
                if b"\n" in data:
                    data = data.split(b"\n", 1)[1]
            else:
                data = stream.read()
    except OSError:
        return []
    records: list[Mapping[str, Any]] = []
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(value, Mapping):
            records.append(value)
    if len(records) > EVENTS_TAIL_RECORDS:
        records = records[-EVENTS_TAIL_RECORDS:]
    return records


def summarize_events(path: Path, now: dt.datetime) -> dict[str, Any]:
    """Summarize one ``events.jsonl`` tail for the monitor.

    ``tools_used`` counts ``tool_execution_start`` events observed in the
    tail. The last start without a matching end is the current tool. The last
    ``message_update``/``message_end`` usage replaces earlier totals; attempts
    are not summed here.
    """
    records = _tail_records(path)
    pending: dict[str, str | None] = {}
    tools_used = 0
    usage: dict[str, Any] | None = None

    for index, event in enumerate(records):
        event_type = event.get("type")
        if event_type == "tool_execution_start":
            identifier = str(
                event.get("toolCallId")
                or event.get("tool_call_id")
                or event.get("id")
                or f"anonymous:{index}"
            )
            name = event.get("toolName") or event.get("name")
            pending[identifier] = str(name) if name is not None else None
            tools_used += 1
        elif event_type == "tool_execution_update":
            identifier = str(
                event.get("toolCallId")
                or event.get("tool_call_id")
                or event.get("id")
                or f"anonymous:{index}"
            )
            if identifier not in pending:
                name = event.get("toolName") or event.get("name")
                pending[identifier] = str(name) if name is not None else None
        elif event_type == "tool_execution_end":
            identifier = event.get("toolCallId") or event.get("tool_call_id") or event.get("id")
            if identifier is not None:
                pending.pop(str(identifier), None)
            else:
                # A malformed end without an id can still close the oldest
                # outstanding call, which is how the Runner executes them.
                if pending:
                    pending.pop(next(iter(pending)))
        elif event_type in {"message_update", "message_end"}:
            event_usage = _usage_from_event(event)
            if event_usage is not None:
                usage = _usage_snapshot(event_usage)

    current_tool = None
    for name in reversed(list(pending.values())):
        if name is not None:
            current_tool = name
            break

    try:
        activity = max(0, int(_aware_utc(now).timestamp() - path.stat().st_mtime))
    except OSError:
        activity = None

    tokens = usage or {}
    token_block = {
        key: value
        for key, value in tokens.items()
        if key
        in {"input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"}
    }
    total_tokens = token_block.get("totalTokens")
    return {
        "session_available": True,
        "tools_used": tools_used,
        "last_activity_sec": activity,
        "current_tool": current_tool,
        "tokens": token_block,
        "total_tokens": total_tokens,
        "tokens_total": total_tokens,
        "cost": tokens.get("cost"),
        "usage": tokens,
    }


def _unavailable_session() -> dict[str, Any]:
    return {
        "session_available": False,
        "tools_used": None,
        "last_activity_sec": None,
        "current_tool": None,
        "tokens": None,
        "total_tokens": None,
        "tokens_total": None,
        "cost": None,
        "usage": None,
    }


def _walk_files(root: Path) -> list[Path]:
    """Return every ``events.jsonl`` path below one identified execution.

    Path discovery is deliberately not capped by a sample of file names: the
    newest file is selected afterwards. Only the contents read from the
    chosen file are bounded. Directory symlinks are never followed.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if not (Path(dirpath) / name).is_symlink()
        )
        if "events.jsonl" not in filenames:
            continue
        candidate = Path(dirpath) / "events.jsonl"
        try:
            if candidate.is_file() and not candidate.is_symlink():
                found.append(candidate)
        except OSError:
            continue
    return found


def _latest(paths: Iterable[Path]) -> Path | None:
    newest: Path | None = None
    newest_mtime = -1.0
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest = path
            newest_mtime = mtime
    return newest


def _task_name_matches(name: str, task_id: str) -> bool:
    return (
        name == task_id
        or name == f"task_{task_id}"
        or name.startswith(f"task_{task_id}_")
        or name.startswith(f"{task_id}_")
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _task_dirs(root: Path, task_id: str) -> list[Path]:
    bases: list[Path] = []
    for candidate in (root / "tasks", root):
        if candidate not in bases:
            bases.append(candidate)
    result: list[Path] = []
    seen: set[Path] = set()
    for base in bases:
        if not base.is_dir():
            continue
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry in seen:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink() and _task_name_matches(
                    entry.name, task_id
                ):
                    seen.add(entry)
                    result.append(entry)
            except OSError:
                continue
    # The active execution directory was created most recently. Search it
    # first, though _events_for_task still compares every candidate file.
    result.sort(key=_mtime, reverse=True)
    return result


def _operation_dirs(root: Path, operation: Operation) -> list[Path]:
    operation_root = root / "operations"
    if operation_root.is_dir():
        bases = [operation_root]
    elif root.name == "operations":
        bases = [root]
    else:
        bases = [root]
    targets: list[Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        direct = [base / operation.id, base / f"operation_{operation.id}"]
        if operation.target_id:
            direct.extend(
                [
                    base / f"task_{operation.target_id}" / f"operation_{operation.id}",
                    base / str(operation.target_id) / f"operation_{operation.id}",
                ]
            )
        for candidate in direct:
            if candidate.is_dir() and candidate not in targets:
                targets.append(candidate)
        # The conventional review path is found directly above. Only when no
        # conventional directory exists, walk the complete tree: a long
        # history must not hide an active operation behind a filename sample.
        if targets:
            continue
        try:
            for dirpath, dirnames, _filenames in os.walk(base, followlinks=False):
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if not (Path(dirpath) / name).is_symlink()
                )
                if operation.id in Path(dirpath).name:
                    candidate = Path(dirpath)
                    if candidate not in targets:
                        targets.append(candidate)
        except OSError:
            continue
    return targets


def _read_head(path: Path, limit: int = MAX_METADATA_HEAD_BYTES) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _read_json_file(path: Path) -> Mapping[str, Any] | None:
    try:
        size = path.stat().st_size
        if size > MAX_ROLE_CONFIG_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _analysis_export_matches(directory: Path, operation_id: str) -> bool:
    """Return whether ``directory`` belongs to this exact analysis operation.

    The analyzer export directory is a random UUID, but the Pi adapter writes
    the exact turn identity (``analysis:<operation_id>``) into each run's
    ``task.txt`` and ``provenance.json``. A directory name alone is not proof;
    matching that marker is the reliable link back to the operation row.
    """
    marker = f"analysis:{operation_id}"
    candidates: list[Path] = []
    direct = (directory / "task.txt", directory / "provenance.json")
    candidates.extend(path for path in direct if path.is_file())
    runs_dir = directory / "runs"
    if runs_dir.is_dir():
        try:
            run_dirs = list(runs_dir.iterdir())
        except OSError:
            run_dirs = []
        for run_dir in run_dirs:
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            for name in ("task.txt", "provenance.json"):
                candidate = run_dir / name
                if candidate.is_file():
                    candidates.append(candidate)
    return any(marker in _read_head(path) for path in candidates)


def _analysis_operation_dirs(root: Path, operation: Operation) -> list[Path]:
    """Find analyzer export directories whose run marker names ``operation``."""
    operation_root = (
        root if root.name == "operations" else root / "operations"
    )
    analysis_root = operation_root / "analysis"
    if not analysis_root.is_dir():
        return []
    try:
        entries = [
            entry
            for entry in analysis_root.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        ]
    except OSError:
        return []
    # The current export is the newest directory. Search recent history first
    # but inspect every entry: a run may not have written task.txt yet.
    entries.sort(key=_mtime, reverse=True)
    return [
        entry
        for entry in entries
        if operation.id in entry.name
        or _analysis_export_matches(entry, operation.id)
    ]


def _state_dirs_for_artifact(directory: Path) -> list[Path]:
    """Return Runner state directories declared by an operation's export."""
    states: list[Path] = []
    seen: set[Path] = set()

    def add(value: Any, base: Path) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            return
        if resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            states.append(resolved)

    role_paths: list[Path] = []
    for candidate in (directory / "runner_role.json",):
        if candidate.is_file():
            role_paths.append(candidate)
    runs_dir = directory / "runs"
    if runs_dir.is_dir():
        try:
            run_dirs = list(runs_dir.iterdir())
        except OSError:
            run_dirs = []
        for run_dir in run_dirs:
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            role_path = run_dir / "runner_role.json"
            if role_path.is_file():
                role_paths.append(role_path)
            provenance = _read_json_file(run_dir / "provenance.json")
            if provenance is not None:
                recorded = provenance.get("role_config_path")
                if isinstance(recorded, str):
                    path = Path(recorded).expanduser()
                    if path.is_file():
                        role_paths.append(path)

    for role_path in role_paths:
        role = _read_json_file(role_path)
        if role is not None:
            add(role.get("state_dir"), role_path.parent)
    return states


def _config_paths_for_artifact(directory: Path) -> set[Path]:
    """Return role-config paths that identify this operation's Runner calls."""
    paths: set[Path] = set()

    def add(value: Any, base: Path) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = base / path
        try:
            paths.add(path.resolve())
        except (OSError, RuntimeError):
            return

    direct = directory / "runner_role.json"
    if direct.is_file():
        add(str(direct), directory)
    runs_dir = directory / "runs"
    if runs_dir.is_dir():
        try:
            run_dirs = list(runs_dir.iterdir())
        except OSError:
            run_dirs = []
        for run_dir in run_dirs:
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            role_path = run_dir / "runner_role.json"
            if role_path.is_file():
                add(str(role_path), run_dir)
            provenance = _read_json_file(run_dir / "provenance.json")
            if provenance is not None:
                role_path = provenance.get("role_config_path")
                if isinstance(role_path, str):
                    add(role_path, run_dir)
    return paths


def _session_files(session_dir: Path) -> list[Path]:
    files: list[Path] = []
    for candidate in (
        session_dir / "task.md",
        session_dir / "pi" / "task.txt",
    ):
        if candidate.is_file():
            files.append(candidate)
    attempts_dir = session_dir / "attempts"
    if attempts_dir.is_dir():
        try:
            attempt_dirs = list(attempts_dir.iterdir())
        except OSError:
            attempt_dirs = []
        for attempt_dir in attempt_dirs:
            if not attempt_dir.is_dir() or attempt_dir.is_symlink():
                continue
            candidate = attempt_dir / "task.md"
            if candidate.is_file():
                files.append(candidate)
    return files


def _session_matches_operation(
    session_dir: Path,
    config_paths: set[Path],
    operation_id: str,
) -> bool:
    """Return whether one Runner session belongs to this exact operation.

    A shared analyzer state directory contains sessions from many
    operations. ``config-path.txt`` names the role config the Runner was
    launched with, and the task header carries the exact turn identity. A
    session is accepted only when those identities agree with the linked
    export; otherwise its events belong to another operation.
    """
    marker = f"analysis:{operation_id}"
    config_match = False
    config_file = session_dir / "config-path.txt"
    if config_file.is_file():
        recorded = _read_head(config_file, 4_096).strip()
        if recorded:
            recorded_path = Path(recorded).expanduser()
            if not recorded_path.is_absolute():
                recorded_path = session_dir / recorded_path
            try:
                config_match = recorded_path.resolve() in config_paths
            except (OSError, RuntimeError):
                config_match = False

    task_files = _session_files(session_dir)
    marker_match = any(marker in _read_head(path) for path in task_files)
    # Every identity that is present must agree. A just-created session may
    # not have written task.md yet; it also cannot have events yet.
    if config_file.is_file() and not config_match:
        return False
    if task_files and not marker_match:
        return False
    return config_match or marker_match


def _latest_attempt_events(session_dir: Path) -> Path | None:
    """Return the newest attempt's events inside one already-linked session."""
    attempts_dir = session_dir / "attempts"
    if attempts_dir.is_dir():
        numbered: list[tuple[int, Path]] = []
        try:
            entries = list(attempts_dir.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink() or not entry.name.isdecimal():
                continue
            numbered.append((int(entry.name), entry))
        if numbered:
            _number, latest = max(numbered, key=lambda item: item[0])
            events = latest / "events.jsonl"
            return events if events.is_file() else None
    # No attempts directory is not the current Runner layout; retain a
    # bounded-compatible discovery fallback for older artifacts.
    return _latest(_walk_files(session_dir))


def _events_in_linked_sessions(
    state_dir: Path,
    config_paths: set[Path],
    operation_id: str,
) -> list[Path]:
    sessions_dir = state_dir / "sessions"
    if not sessions_dir.is_dir():
        return []
    try:
        sessions = list(sessions_dir.iterdir())
    except OSError:
        return []
    events: list[Path] = []
    for session_dir in sessions:
        if not session_dir.is_dir() or session_dir.is_symlink():
            continue
        if not _session_matches_operation(session_dir, config_paths, operation_id):
            continue
        latest = _latest_attempt_events(session_dir)
        if latest is not None:
            events.append(latest)
    return events


def _operation_artifact_dirs(root: Path, operation: Operation) -> list[Path]:
    directories: list[Path] = []
    seen: set[Path] = set()
    for candidate in (
        *_operation_dirs(root, operation),
        *_analysis_operation_dirs(root, operation),
    ):
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            resolved = candidate
        if resolved not in seen:
            seen.add(resolved)
            directories.append(candidate)
    return directories


def _events_for_task(root: Path, task: Task) -> Path | None:
    paths: list[Path] = []
    for directory in _task_dirs(root, str(task.id)):
        paths.extend(_walk_files(directory))
    return _latest(paths)


def _events_for_operation(root: Path, operation: Operation) -> Path | None:
    """Return events only from artifacts provably linked to ``operation``.

    There is deliberately no fallback to the newest event file in the whole
    operations tree. Such a fallback would attribute a concurrent or
    completed review/analysis to an unrelated running operation.
    """
    paths: list[Path] = []
    for directory in _operation_artifact_dirs(root, operation):
        paths.extend(_walk_files(directory))
        config_paths = _config_paths_for_artifact(directory)
        for state_dir in _state_dirs_for_artifact(directory):
            paths.extend(
                _events_in_linked_sessions(
                    state_dir, config_paths, operation.id
                )
            )
    return _latest(paths)


def _queue_counts(database: Database, tasks: list[Task]) -> dict[str, int]:
    with database.session() as session:
        turns_pending = int(
            session.scalar(
                sa.select(sa.func.count()).select_from(Turn).where(
                    Turn.status == "pending"
                )
            )
            or 0
        )
        operations_queued = int(
            session.scalar(
                sa.select(sa.func.count()).select_from(Operation).where(
                    Operation.status.in_(sorted(OPERATION_QUEUED_STATUSES))
                )
            )
            or 0
        )
        subscriptions_queued = int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(TaskSubscription)
                .where(TaskSubscription.status == "queued")
            )
            or 0
        )
    tasks_unclaimed = sum(
        1
        for task in tasks
        if task.status == "in_progress"
        and (task.meta or {}).get("execution_status") != "running"
    )
    return {
        "turns": turns_pending,
        "operations": operations_queued,
        "subscriptions": subscriptions_queued,
        "tasks": tasks_unclaimed,
        "tasks_unclaimed": tasks_unclaimed,
        "turns_pending": turns_pending,
        "operations_pending": operations_queued,
        "subscriptions_queued": subscriptions_queued,
        "tasks_in_progress_unclaimed": tasks_unclaimed,
    }


def _task_entry(
    task: Task,
    events_path: Path | None,
    now: dt.datetime,
) -> dict[str, Any]:
    meta = dict(task.meta or {})
    session = summarize_events(events_path, now) if events_path else _unavailable_session()
    claimed_at = meta.get("execution_claimed_at")
    elapsed = _elapsed_seconds(claimed_at or task.updated_at, now)
    number = task.number if task.number is not None else task.id
    total_tokens = (
        session["tokens"].get("totalTokens")
        if isinstance(session.get("tokens"), Mapping)
        else None
    )
    return {
        "kind": "task",
        "entry_type": "task",
        "task_id": task.id,
        "id": task.id,
        "number": task.number,
        "title": task.title,
        "agent_id": task.agent_id or meta.get("execution_agent_id"),
        "workspace": task.workspace,
        "status": task.status,
        "claimed_at": claimed_at,
        "elapsed": elapsed,
        "elapsed_sec": elapsed,
        **session,
        "total_tokens": total_tokens,
        "tokens_total": total_tokens,
        "events_path": str(events_path) if events_path else None,
        "label": f"#{number}",
    }


def _operation_entry(
    operation: Operation,
    task_by_id: Mapping[str, Task],
    events_path: Path | None,
    now: dt.datetime,
) -> dict[str, Any]:
    target = task_by_id.get(str(operation.target_id or ""))
    session = summarize_events(events_path, now) if events_path else _unavailable_session()
    elapsed = _elapsed_seconds(operation.claimed_at or operation.updated_at, now)
    target_number = (
        target.number
        if target is not None and target.number is not None
        else (target.id if target is not None else operation.target_id)
    )
    total_tokens = (
        session["tokens"].get("totalTokens")
        if isinstance(session.get("tokens"), Mapping)
        else None
    )
    return {
        "kind": operation.kind,
        "entry_type": "operation",
        "operation_id": operation.id,
        "id": operation.id,
        "operation_kind": operation.kind,
        "kind_label": OPERATION_LABELS.get(operation.kind, operation.kind),
        "agent_id": OPERATION_AGENTS.get(operation.kind),
        "target_id": operation.target_id,
        "target_number": target_number,
        "target_title": target.title if target is not None else None,
        "target": {
            "id": operation.target_id,
            "number": target_number,
            "title": target.title if target is not None else None,
        },
        "claimed_at": operation.claimed_at.isoformat() if operation.claimed_at else None,
        "elapsed": elapsed,
        "elapsed_sec": elapsed,
        **session,
        "total_tokens": total_tokens,
        "tokens_total": total_tokens,
        "events_path": str(events_path) if events_path else None,
        "label": f"#{target_number}" if target_number is not None else operation.kind,
    }


def build_execution_monitor(
    database: Database,
    artifact_root: str | os.PathLike[str] | None,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe snapshot of currently running executions and queues."""
    moment = now or _utcnow()
    root = Path(artifact_root or "~/.kuzya/data/artifacts").expanduser()

    with database.session() as session:
        tasks = list(session.scalars(sa.select(Task)))
    tasks_by_id = {str(task.id): task for task in tasks}
    running_tasks = [
        task
        for task in tasks
        if (task.meta or {}).get("execution_status") == "running"
    ]
    with database.session() as session:
        running_operations = list(
            session.scalars(
                sa.select(Operation)
                .where(Operation.status == OPERATION_STATUS_RUNNING)
                .order_by(Operation.claimed_at, Operation.sequence)
            )
        )

    task_events: dict[str, Path | None] = {
        str(task.id): _events_for_task(root, task) for task in running_tasks
    }
    operation_events: dict[str, Path | None] = {
        operation.id: _events_for_operation(root, operation)
        for operation in running_operations
    }

    running: list[dict[str, Any]] = []
    for task in running_tasks:
        running.append(
            _task_entry(task, task_events.get(str(task.id)), moment)
        )
    for operation in running_operations:
        running.append(
            _operation_entry(
                operation,
                tasks_by_id,
                operation_events.get(operation.id),
                moment,
            )
        )

    return {
        "generated_at": _aware_utc(moment).isoformat().replace("+00:00", "Z"),
        "running": running,
        "queues": _queue_counts(database, tasks),
    }


__all__ = [
    "OPERATION_LABELS",
    "build_execution_monitor",
    "summarize_events",
]
