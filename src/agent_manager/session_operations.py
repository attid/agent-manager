"""Acknowledge deferred session records without deleting their audit history."""

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path


def unapplied_items(path: Path) -> Iterator[dict]:
    """Yield unacknowledged records; acknowledge only after their body succeeds.

    An interrupted body is retried. Consumers must make the individual write
    idempotent as a crash can occur between applying it and this acknowledgement.
    """
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise TypeError(f"Expected a JSON array in {path.name}")
    receipt = path.with_name(path.stem + ".applied.json")
    applied = set(json.loads(receipt.read_text())) if receipt.exists() else set()
    for index, record in enumerate(records):
        key = hashlib.sha256(
            json.dumps([index, record], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if key in applied:
            continue
        yield record
        applied.add(key)
        temporary = receipt.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(sorted(applied)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(receipt)
