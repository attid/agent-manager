"""Read-only projection of persisted review rounds for one task.

The review executor writes a structured metrics document next to the
human-readable report for every completed round::

    <artifact dir>/review/round_<n>.json
    <artifact dir>/review/round_<n>.md

Review artifacts live in three historical layouts, and all of them remain
supported so an operator does not have to read the database or guess which
worker produced a task:

* ``<artifact_root>/tasks/<task_id>/review`` from the in-worker review cycle;
* ``<artifact_root>/<task_id>/review`` from the older automatic layout;
* ``<artifact_root>/operations/task_<task_id>/operation_<id>/review`` from the
  standalone review operation.

Some layouts add a ``task_`` prefix to the directory name. That prefix is
ambiguous: a task literally named ``task_alpha`` owns a directory with the
same name that a request for task ``alpha`` might otherwise read. Every
artifact from a prefixed directory must therefore carry a matching
``task_id`` in its JSON document; an artifact without that proof is skipped.
Exact-id directories are trusted by their path, but a recorded ``task_id``
that contradicts the requested task is still skipped.

Only metadata and the report text are exposed. The module never follows
directory symlinks and never reads a path recorded inside an artifact.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _ReviewDirectory:
    """One candidate directory and whether its path proves task ownership."""

    path: Path
    exact: bool


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _naive_utc_from_timestamp(value: float) -> dt.datetime | None:
    """Convert a POSIX timestamp, or return ``None`` when it is unusable."""
    try:
        moment = dt.datetime.fromtimestamp(value, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.replace(tzinfo=None)


def _timestamp_from_artifact(
    data: Mapping[str, Any], path: Path
) -> dt.datetime:
    """Return the artifact timestamp, falling back to the file mtime.

    A corrupted metrics file can carry ``NaN``, infinity or a value outside
    the representable datetime range. One such value must not turn the whole
    review listing into a ``500``, so conversion failures fall back to mtime.
    """
    fallback = _naive_utc_from_timestamp(_mtime(path)) or dt.datetime(
        1970, 1, 1
    )
    raw = data.get("created_at")
    if raw is None:
        raw = data.get("timestamp")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            numeric = float(raw)
        except (OverflowError, ValueError):
            return fallback
        return _naive_utc_from_timestamp(numeric) or fallback
    if isinstance(raw, str):
        try:
            moment = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
            return moment
        except (OverflowError, OSError, ValueError):
            return fallback
    return fallback


def _safe_task_component(task_id: str) -> bool:
    """Return whether ``task_id`` can safely name an artifact directory."""
    return bool(task_id) and task_id not in {".", ".."} and not any(
        separator in task_id for separator in ("/", "\\", "\x00")
    )


def _iter_directories(path: Path) -> list[Path]:
    if not path.is_dir() or path.is_symlink():
        return []
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    result: list[Path] = []
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                result.append(entry)
        except OSError:
            continue
    return result


def _add_once(
    target: list[_ReviewDirectory],
    candidate: Path,
    seen: set[Path],
    *,
    exact: bool,
) -> None:
    if candidate in seen:
        return
    seen.add(candidate)
    target.append(_ReviewDirectory(path=candidate, exact=exact))


def _review_directories(root: Path, task_id: str) -> list[_ReviewDirectory]:
    if not _safe_task_component(task_id):
        return []

    directories: list[_ReviewDirectory] = []
    seen: set[Path] = set()
    prefixed = f"task_{task_id}"

    # In-worker review cycle. The stable directory is exact-id; the prefixed
    # variant is kept for older trees but requires an ownership check because
    # ``task_<id>`` can also be another task's literal id.
    tasks_root = root / "tasks"
    _add_once(
        directories, tasks_root / task_id / "review", seen, exact=True
    )
    _add_once(
        directories, tasks_root / prefixed / "review", seen, exact=False
    )

    # Older automatic layout, including an artifact root that already points
    # at the task directory itself.
    _add_once(directories, root / task_id / "review", seen, exact=True)
    _add_once(directories, root / prefixed / "review", seen, exact=False)
    if root.name == task_id:
        _add_once(directories, root / "review", seen, exact=True)
    elif root.name == prefixed:
        _add_once(directories, root / "review", seen, exact=False)

    # Standalone review operation:
    # <root>/operations/task_<task_id>/operation_<id>/review. When the
    # configured artifact root already is the operations directory, use it
    # directly.
    operation_roots = [root / "operations"]
    if root.name == "operations":
        operation_roots.append(root)
    for operations_root in operation_roots:
        for name, exact in ((task_id, True), (prefixed, False)):
            task_operations = operations_root / name
            if not task_operations.is_dir() or task_operations.is_symlink():
                continue
            _add_once(
                directories,
                task_operations / "review",
                seen,
                exact=exact,
            )
            for operation_dir in _iter_directories(task_operations):
                if operation_dir.name.startswith("operation_"):
                    _add_once(
                        directories,
                        operation_dir / "review",
                        seen,
                        exact=exact,
                    )

    return directories


def review_directories(artifact_root: Path, task_id: str) -> list[Path]:
    """Return candidate ``review`` directories for ``task_id``.

    Candidate directories are not required to exist. They are deduplicated
    but never resolved, so a symlinked directory is not silently followed.
    """
    root = Path(artifact_root).expanduser()
    return [
        item.path for item in _review_directories(root, task_id)
    ]


def _round_number(data: Mapping[str, Any], filename_number: int | None) -> int | None:
    value = data.get("round")
    if isinstance(value, bool):
        value = None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value.strip())
    return filename_number


def _verdict(data: Mapping[str, Any]) -> str | None:
    domain = data.get("domain_verdict")
    if isinstance(domain, str) and domain.strip():
        return domain.strip().lower()
    raw = data.get("verdict")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    return None


def _findings(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("findings")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _artifact_belongs_to_task(
    data: Mapping[str, Any],
    task_id: str,
    *,
    exact: bool,
) -> bool:
    """Return whether an artifact may be attributed to the requested task.

    A recorded ``task_id`` is authoritative: a mismatch is rejected even in
    an exact-id directory. Without a recorded identity, only a directory
    whose name is exactly the task id proves ownership. A prefixed directory
    without that proof is ambiguous and is rejected.
    """
    recorded = data.get("task_id")
    if recorded is not None:
        return str(recorded) == task_id
    return exact


def _report_text(path: Path, data: Mapping[str, Any]) -> str:
    report_path = path.with_suffix(".md")
    if report_path.is_file() and not report_path.is_symlink():
        try:
            return report_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    embedded = data.get("report")
    return embedded if isinstance(embedded, str) else ""


def list_review_rounds(
    artifact_root: str | os.PathLike[str] | None,
    task_id: str,
) -> list[dict[str, Any]]:
    """Return persisted review rounds for ``task_id`` in ascending order.

    A malformed or incomplete ``round_<n>.json`` is skipped with a warning:
    one corrupt artifact must not hide every other round of the task. Rounds
    from different review executions are all returned; equal round numbers
    are ordered by ``created_at`` and then by path.
    """
    if not task_id:
        return []
    root = Path(artifact_root or "~/.kuzya/data/artifacts").expanduser()
    rounds: list[dict[str, Any]] = []
    for candidate in _review_directories(root, task_id):
        directory = candidate.path
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file() or path.is_symlink():
                continue
            if not path.name.startswith("round_") or path.suffix != ".json":
                continue
            stem = path.stem[len("round_") :]
            filename_number = (
                int(stem) if stem.isdecimal() else None
            )
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
                logger.warning(
                    "Skipping unreadable review artifact %s: %s", path, error
                )
                continue
            if not isinstance(data, Mapping):
                logger.warning(
                    "Skipping review artifact %s: top-level JSON is not an object",
                    path,
                )
                continue
            if not _artifact_belongs_to_task(
                data, task_id, exact=candidate.exact
            ):
                logger.warning(
                    "Skipping review artifact %s: it does not prove ownership "
                    "by task %s",
                    path,
                    task_id,
                )
                continue
            number = _round_number(data, filename_number)
            verdict = _verdict(data)
            if number is None or verdict is None:
                logger.warning(
                    "Skipping review artifact %s: missing round or verdict",
                    path,
                )
                continue
            rounds.append(
                {
                    "round": number,
                    "verdict": verdict,
                    "findings": _findings(data),
                    "report": _report_text(path, data),
                    "created_at": _timestamp_from_artifact(data, path),
                    "_source": str(path),
                }
            )

    rounds.sort(
        key=lambda item: (
            int(item["round"]),
            item["created_at"],
            str(item["_source"]),
        )
    )
    for item in rounds:
        item.pop("_source", None)
    return rounds


__all__ = ["list_review_rounds", "review_directories"]
