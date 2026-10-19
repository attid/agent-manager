"""Unit tests for parsing the Pi Runner CLI JSON result.

The real Runner CLI prints one indented multi-line JSON document. Callers that
captured its output must accept that document; a single-line scan turns a
successful execution into an unparseable failure.
"""

from __future__ import annotations

import json

from agent_manager.runner_output import extract_runner_result


def test_parses_pretty_printed_runner_result() -> None:
    payload = {
        "status": "completed",
        "changed_files": ["index.html"],
        "summary_path": "/tmp/summary.md",
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    assert extract_runner_result(text) == payload


def test_parses_last_balanced_object_after_noise() -> None:
    payload = {"status": "completed", "changed_files": []}
    text = "warning: something happened\n" + json.dumps(payload, indent=2) + "\n"
    assert extract_runner_result(text) == payload


def test_rejects_arrays_scalars_and_garbage() -> None:
    assert extract_runner_result("[]") is None
    assert extract_runner_result('"completed"') is None
    assert extract_runner_result("not json at all") is None
    assert extract_runner_result("") is None
    assert extract_runner_result(None) is None
