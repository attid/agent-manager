"""Metadata-only listing of a Runner state directory's sessions.

The Runner stores one directory per native session under
``<state_dir>/sessions/<session_id>/`` and one numbered directory per attempt
under ``attempts/``. This module exposes only the session id, attempt count
and attempt timestamps. It deliberately never opens ``events.jsonl``,
``task.md`` or any other file: session contents are private model data and
are not part of the operational API contract.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

__all__ = ["list_agent_sessions", "sessions_directory"]

#: Subdirectory of a Runner state directory that holds native sessions.
SESSIONS_DIRECTORY = "sessions"

#: Subdirectory of a session that holds numbered attempts.
ATTEMPTS_DIRECTORY = "attempts"


def sessions_directory(state_dir: str | Path) -> Path | None:
    """Return the sessions directory for a trusted ``state_dir``.

    ``None`` means the state directory cannot be read safely: it is empty,
    relative after ``expanduser`` or points at a symlink. A missing
    ``sessions`` directory is returned as a path so callers can report an
    empty list without inventing one.
    """
    raw = str(state_dir or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or path.is_symlink():
        return None
    return path / SESSIONS_DIRECTORY


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _naive_utc(value: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).replace(tzinfo=None)


def _attempts(session_dir: Path) -> list[tuple[int, Path]]:
    attempts_dir = session_dir / ATTEMPTS_DIRECTORY
    if not attempts_dir.is_dir() or attempts_dir.is_symlink():
        return []
    try:
        entries = list(attempts_dir.iterdir())
    except OSError:
        return []
    numbered: list[tuple[int, Path]] = []
    for entry in entries:
        try:
            if (
                entry.is_dir()
                and not entry.is_symlink()
                and entry.name.isdecimal()
            ):
                numbered.append((int(entry.name), entry))
        except OSError:
            continue
    numbered.sort(key=lambda item: item[0])
    return numbered


def list_agent_sessions(state_dir: str | Path) -> list[dict[str, Any]]:
    """Return session metadata ordered by most recent activity first.

    ``created_at`` is the mtime of the first (lowest-numbered) attempt and
    ``last_activity`` the mtime of the last (highest-numbered) attempt. A
    freshly created session without attempts falls back to the session
    directory mtime for both values and reports ``attempts_count=0``.
    """
    directory = sessions_directory(state_dir)
    if directory is None or not directory.is_dir() or directory.is_symlink():
        return []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []

    sessions: list[dict[str, Any]] = []
    for session_dir in entries:
        try:
            if not session_dir.is_dir() or session_dir.is_symlink():
                continue
        except OSError:
            continue
        attempts = _attempts(session_dir)
        if attempts:
            created = _mtime(attempts[0][1])
            last_activity = _mtime(attempts[-1][1])
        else:
            created = last_activity = _mtime(session_dir)
        sessions.append(
            {
                "session_id": session_dir.name,
                "created_at": _naive_utc(created),
                "last_activity": _naive_utc(last_activity),
                "attempts_count": len(attempts),
            }
        )

    sessions.sort(
        key=lambda item: (item["last_activity"], item["session_id"]),
        reverse=True,
    )
    return sessions
