"""Acknowledge deferred session records without deleting their audit history."""

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path


def _load_state(path: Path) -> tuple[list, Path, set[str]]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise TypeError(f"Expected a JSON array in {path.name}")
    receipt = path.with_name(path.stem + ".applied.json")
    applied = set(json.loads(receipt.read_text())) if receipt.exists() else set()
    return records, receipt, applied


def _record_key(index: int, record: object) -> str:
    return hashlib.sha256(
        json.dumps([index, record], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _write_receipt(receipt: Path, applied: set[str]) -> None:
    temporary = receipt.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(sorted(applied)) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(receipt)


def unapplied_items(path: Path) -> Iterator[dict]:
    """Yield unacknowledged records; acknowledge only after their body succeeds.

    An interrupted body is retried. Consumers must make the individual write
    idempotent as a crash can occur between applying it and this acknowledgement.
    """
    records, receipt, applied = _load_state(path)
    for index, record in enumerate(records):
        key = _record_key(index, record)
        if key in applied:
            continue
        yield record
        applied.add(key)
        _write_receipt(receipt, applied)


def pending_items(
    path: Path, *, match: Callable[[dict], bool] | None = None
) -> list[tuple[int, dict]]:
    """Return unacknowledged records without acknowledging them.

    The optional ``match`` predicate selects only the records the caller can
    deliver now. The returned index/record pairs are passed to
    :func:`acknowledge_items` after the caller has durably persisted the
    effect, so a failure in between leaves the record pending for a retry.
    """
    records, _, applied = _load_state(path)
    return [
        (index, record)
        for index, record in enumerate(records)
        if _record_key(index, record) not in applied
        and (match is None or match(record))
    ]


def acknowledge_items(path: Path, items: Sequence[tuple[int, dict]]) -> None:
    """Acknowledge records whose body has already completed."""
    if not items:
        return
    _, receipt, applied = _load_state(path)
    for index, record in items:
        applied.add(_record_key(index, record))
    _write_receipt(receipt, applied)
