"""Wall-clock context injected into agent prompts."""

from __future__ import annotations

import re

from kuzya.time_context import TIMEZONE_ENV, time_context_line


def test_env_timezone_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv(TIMEZONE_ENV, "Europe/Moscow")
    line = time_context_line()
    assert "UTC+03:00" in line
    assert "Europe/Moscow" in line
    assert "naive UTC" in line


def test_system_timezone_fallback_format(monkeypatch) -> None:
    monkeypatch.delenv(TIMEZONE_ENV, raising=False)
    line = time_context_line()
    # Whatever the system zone is, the line stays parseable and explicit.
    assert re.search(r"Текущее время: \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC[+-]\d{2}:\d{2}", line)
    assert "переводи" in line


def test_invalid_timezone_falls_back(monkeypatch) -> None:
    monkeypatch.setenv(TIMEZONE_ENV, "Not/AZone")
    line = time_context_line()
    assert "Текущее время:" in line  # still renders, system zone


def test_wall_clock_line_reaches_secretary_task_text() -> None:
    from kuzya.pi_reply import _render_task_text
    from kuzya.secretary import TurnInput

    task_text = _render_task_text(
        TurnInput(
            turn_id="t1",
            conversation_id="c1",
            input_message_id="m1",
            text="который час?",
            attempt_count=1,
            role="role.json",
            metadata={},
        )
    )
    assert "wall_clock:" in task_text
    assert "naive UTC" in task_text
