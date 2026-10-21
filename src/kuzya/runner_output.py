"""Parse the JSON result object printed by the Pi Runner CLI.

The Runner CLI prints exactly one JSON result document to stdout, and it is
pretty-printed across multiple lines (``json.dumps(..., indent=2)``). Callers
that capture that output as a subprocess used to scan for a *single* line that
started and ended with a brace; the real CLI output has no such line, so a
successful execution was reported as an unparseable failure.

This helper accepts the whole captured document first (the normal case) and,
for tolerance against accidental prefix lines, falls back to the last balanced
JSON object found in the text. It never parses non-object JSON as a result.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["extract_runner_result"]


def _balanced_object_at(text: str, start: int) -> str | None:
    """Return the balanced ``{...}`` substring beginning at ``start``.

    Brace matching is depth-aware and ignores braces inside JSON strings. It
    returns ``None`` when the object never closes or a string never
    terminates.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_runner_result(stdout: str | None) -> dict[str, Any] | None:
    """Return the last JSON object printed on ``stdout``, or ``None``.

    The whole stripped output is tried first. When it is not a single JSON
    document, the last balanced JSON object anywhere in the text is returned.
    Only JSON objects are accepted; arrays, scalars and malformed text yield
    ``None``.
    """
    if not stdout:
        return None
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    for start in range(len(text) - 1, -1, -1):
        if text[start] != "{":
            continue
        candidate = _balanced_object_at(text, start)
        if candidate is None:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
