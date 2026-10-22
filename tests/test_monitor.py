"""Live execution monitor acceptance tests.

Every test uses a real migrated file database and a temporary artifact tree.
Event files are synthetic JSONL tails shaped like the Runner output, so the
tests exercise the same parser the endpoint uses without launching a model.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time

from fastapi.testclient import TestClient

from kuzya.api import create_app
from kuzya.monitor import build_execution_monitor, summarize_events
from kuzya.permissions import ExecutionPermissions, resolve_requirement
from kuzya.storage import (
    Database,
    OperationRepository,
    TaskRepository,
)


def _iso(seconds_ago: int) -> str:
    moment = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(
        seconds=seconds_ago
    )
    return moment.isoformat()


def _write_events(path: pathlib.Path, records: list[dict]) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_linked_session(
    state_dir: pathlib.Path,
    session_id: str,
    config_path: pathlib.Path,
    turn_id: str,
    records: list[dict],
) -> pathlib.Path:
    session = state_dir / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "config-path.txt").write_text(str(config_path), encoding="utf-8")
    (session / "task.md").write_text(f"turn_id: {turn_id}\n", encoding="utf-8")
    return _write_events(session / "attempts" / "1" / "events.jsonl", records)


def _seed_running_work(
    migrated_db: pathlib.Path, artifact_root: pathlib.Path
) -> tuple[Database, str, str, pathlib.Path, pathlib.Path]:
    database = Database.from_path(migrated_db)
    tasks = TaskRepository(database)
    task = tasks.create(
        title="Check live monitor",
        status="in_progress",
        agent_id="developer",
        workspace="/workspace",
    )
    task = tasks.update(
        task.id,
        metadata={
            "execution_status": "running",
            "execution_worker_id": "worker-1",
            "execution_claimed_at": _iso(360),
        },
    )
    operations = OperationRepository(database)
    operation = operations.submit(
        kind="analysis", target_id=task.id, input={}
    ).operation
    claimed = operations.claim_next(worker_id="operation-worker-1")
    assert claimed is not None and claimed.id == operation.id

    task_events = _write_events(
        artifact_root
        / "tasks"
        / f"task_{task.id}_deadbeef"
        / "state"
        / "sessions"
        / "session-1"
        / "attempts"
        / "1"
        / "events.jsonl",
        [
            {"type": "tool_execution_start", "toolCallId": "1", "toolName": "read"},
            {"type": "tool_execution_end", "toolCallId": "1", "toolName": "read"},
            {"type": "tool_execution_start", "toolCallId": "2", "toolName": "bash"},
            {
                "type": "message_update",
                "usage": {
                    "input": 100,
                    "output": 38,
                    "cacheRead": 0,
                    "totalTokens": 138000,
                    "cost": {"total": 0.13},
                },
            },
        ],
    )
    operation_events = _write_events(
        artifact_root
        / "operations"
        / f"task_{task.id}"
        / f"operation_{operation.id}"
        / "state"
        / "sessions"
        / "session-2"
        / "attempts"
        / "1"
        / "events.jsonl",
        [
            {
                "type": "tool_execution_start",
                "toolCallId": "op-1",
                "toolName": "read_knowledge",
            },
            {
                "type": "message_update",
                "usage": {
                    "input": 10,
                    "output": 5,
                    "totalTokens": 15,
                    "cost": {"total": 0.002},
                },
            },
        ],
    )
    # Deterministic activity ages: task = 7s, operation = 2s.
    now = dt.datetime.now().timestamp()
    os.utime(task_events, (now - 7, now - 7))
    os.utime(operation_events, (now - 2, now - 2))
    return database, task.id, operation.id, task_events, operation_events


def test_monitor_reports_running_task_and_operation_with_live_stats(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    database, task_id, operation_id, _task_events, _operation_events = (
        _seed_running_work(migrated_db, artifact_root)
    )
    try:
        snapshot = build_execution_monitor(database, artifact_root)
    finally:
        database.dispose()

    running = snapshot["running"]
    task_entry = next(item for item in running if item["entry_type"] == "task")
    operation_entry = next(
        item for item in running if item["entry_type"] == "operation"
    )

    assert task_entry["task_id"] == task_id
    assert task_entry["title"] == "Check live monitor"
    assert task_entry["agent_id"] == "developer"
    assert task_entry["workspace"] == "/workspace"
    assert task_entry["elapsed"] >= 359
    assert task_entry["tools_used"] == 2
    assert task_entry["current_tool"] == "bash"
    assert task_entry["tokens"]["totalTokens"] == 138000
    assert task_entry["cost"] == 0.13
    assert 0 <= task_entry["last_activity_sec"] <= 30
    assert task_entry["session_available"] is True

    assert operation_entry["operation_id"] == operation_id
    assert operation_entry["kind"] == "analysis"
    assert operation_entry["entry_type"] == "operation"
    assert operation_entry["operation_kind"] == "analysis"
    assert operation_entry["kind_label"] == "Анализ"
    assert operation_entry["target_id"] == task_id
    assert operation_entry["target_number"] == task_entry["number"]
    assert operation_entry["target_title"] == "Check live monitor"
    assert operation_entry["elapsed"] >= 0
    assert operation_entry["tools_used"] == 1
    assert operation_entry["current_tool"] == "read_knowledge"
    assert operation_entry["tokens"]["totalTokens"] == 15
    assert operation_entry["cost"] == 0.002
    assert operation_entry["session_available"] is True

    assert snapshot["queues"]["tasks"] == 0
    assert snapshot["queues"]["turns"] == 0

    # The same snapshot is served by the authenticated HTTP route.
    app = create_app(
        f"sqlite:///{migrated_db}",
        api_tokens={"admin-token"},
        artifact_root=artifact_root,
    )
    with TestClient(app) as client:
        response = client.get(
            "/monitor/executions",
            headers={"Authorization": "Bearer admin-token"},
        )
    assert response.status_code == 200
    http_running = response.json()["running"]
    assert {item["entry_type"] for item in http_running} == {"task", "operation"}
    http_task = next(item for item in http_running if item["entry_type"] == "task")
    assert http_task["tokens"]["totalTokens"] == 138000
    assert http_task["current_tool"] == "bash"


def test_monitor_degrades_without_event_file(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        task = tasks.create(title="No artifacts yet", status="in_progress")
        task = tasks.update(
            task.id,
            metadata={
                "execution_status": "running",
                "execution_claimed_at": _iso(12),
            },
        )
        snapshot = build_execution_monitor(database, tmp_path / "missing")
    finally:
        database.dispose()

    [entry] = snapshot["running"]
    assert entry["task_id"] == task.id
    assert entry["elapsed"] >= 11
    assert entry["session_available"] is False
    assert entry["tools_used"] is None
    assert entry["last_activity_sec"] is None
    assert entry["current_tool"] is None
    assert entry["tokens"] is None
    assert entry["cost"] is None


def test_monitor_uses_only_the_newest_attempt_events(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    artifact_root = tmp_path / "artifacts"
    try:
        tasks = TaskRepository(database)
        task = tasks.create(title="Resumed execution", status="in_progress")
        task = tasks.update(
            task.id,
            metadata={
                "execution_status": "running",
                "execution_claimed_at": _iso(60),
            },
        )
        base = (
            artifact_root
            / "tasks"
            / f"task_{task.id}_cafebabe"
            / "state"
            / "sessions"
            / "session-1"
            / "attempts"
        )
        first = _write_events(
            base / "1" / "events.jsonl",
            [
                {"type": "tool_execution_start", "toolCallId": "a", "toolName": "old"},
                {"type": "tool_execution_end", "toolCallId": "a"},
                {
                    "type": "message_update",
                    "usage": {"totalTokens": 1, "cost": {"total": 0.001}},
                },
            ],
        )
        second = _write_events(
            base / "2" / "events.jsonl",
            [
                {"type": "tool_execution_start", "toolCallId": "b", "toolName": "new"},
                {
                    "type": "message_update",
                    "usage": {"totalTokens": 999, "cost": {"total": 0.02}},
                },
            ],
        )
        now = dt.datetime.now().timestamp()
        os.utime(first, (now - 100, now - 100))
        os.utime(second, (now - 1, now - 1))

        snapshot = build_execution_monitor(database, artifact_root)
    finally:
        database.dispose()

    [entry] = snapshot["running"]
    assert entry["tools_used"] == 1
    assert entry["current_tool"] == "new"
    assert entry["tokens"]["totalTokens"] == 999
    assert entry["cost"] == 0.02


def test_monitor_endpoint_requires_admin_and_collection_scope(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    permissions = ExecutionPermissions()
    app = create_app(
        f"sqlite:///{migrated_db}",
        permissions=permissions,
        artifact_root=tmp_path / "artifacts",
    )
    scoped = permissions.issue(
        agent_id="developer",
        actions=["tasks.read"],
        resources=["task:task-1"],
    )
    with TestClient(app) as client:
        assert client.get("/monitor/executions").status_code == 401
        denied = client.get(
            "/monitor/executions",
            headers={"Authorization": f"Bearer {scoped.token}"},
        )
        assert denied.status_code == 403

    admin_app = create_app(
        f"sqlite:///{migrated_db}",
        api_tokens={"admin-token"},
        artifact_root=tmp_path / "artifacts",
    )
    with TestClient(admin_app) as client:
        response = client.get(
            "/monitor/executions",
            headers={"Authorization": "Bearer admin-token"},
        )
    assert response.status_code == 200
    assert response.json()["running"] == []
    assert "queues" in response.json()


def test_monitor_route_is_a_task_read_collection_rule() -> None:
    requirement = resolve_requirement("GET", "/monitor/executions")
    assert requirement is not None
    assert requirement.action == "tasks.read"
    assert requirement.resource is None
    assert requirement.resource_kind is None
    assert requirement.deferred is False

def _running_task(database: Database, title: str):
    tasks = TaskRepository(database)
    task = tasks.create(title=title, status="in_progress")
    return tasks.update(
        task.id,
        metadata={
            "execution_status": "running",
            "execution_claimed_at": _iso(30),
        },
    )


def test_analysis_session_is_linked_by_turn_marker_without_review_leak(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Concurrent review events must never become an analysis session."""
    database = Database.from_path(migrated_db)
    root = tmp_path / "artifacts"
    try:
        task = _running_task(database, "Analysis linkage")
        operations = OperationRepository(database)
        linked = operations.submit(
            kind="analysis", target_id=task.id, input={}
        ).operation
        missing = operations.submit(
            kind="analysis", target_id=task.id, input={}
        ).operation
        review = operations.submit(
            kind="review", target_id=task.id, input={}
        ).operation
        claimed = [operations.claim_next(worker_id="w") for _ in range(3)]
        assert {item.id for item in claimed if item is not None} == {
            linked.id,
            missing.id,
            review.id,
        }

        # Real analyzer structure: a random export directory whose run task
        # carries the exact turn identity, with Runner events in the external
        # state_dir declared by runner_role.json.
        export = root / "operations" / "analysis" / "random-analyzer-uuid"
        run_dir = export / "runs" / "20260101-000000-run"
        run_dir.mkdir(parents=True)
        (run_dir / "task.txt").write_text(
            f"turn_id: analysis:{linked.id}\nconversation_id: analysis:{task.id}\n"
            "\nAnalyze this task\n",
            encoding="utf-8",
        )
        analyzer_state = tmp_path / "analyzer-state"
        (export / "runner_role.json").write_text(
            json.dumps({"state_dir": str(analyzer_state)}),
            encoding="utf-8",
        )
        _write_linked_session(
            analyzer_state,
            "session-linked",
            export / "runner_role.json",
            f"analysis:{linked.id}",
            [
                {"type": "tool_execution_start", "toolCallId": "a", "toolName": "read"},
                {"type": "tool_execution_end", "toolCallId": "a"},
                {
                    "type": "tool_execution_start",
                    "toolCallId": "b",
                    "toolName": "analyze",
                },
                {
                    "type": "message_update",
                    "usage": {"totalTokens": 111, "cost": {"total": 0.01}},
                },
            ],
        )

        # The second analysis has only its discovery directory and no session.
        (root / "operations" / "analysis" / f"discovery_{missing.id}").mkdir(
            parents=True
        )

        # A foreign, fresher analyzer export has no marker for any running
        # operation; the old fallback would have borrowed its numbers.
        foreign = root / "operations" / "analysis" / "foreign-completed-uuid"
        foreign_run = foreign / "runs" / "20260101-later-run"
        foreign_run.mkdir(parents=True)
        (foreign_run / "task.txt").write_text(
            "turn_id: analysis:completed-operation\n", encoding="utf-8"
        )
        _write_events(
            foreign / "state" / "sessions" / "s" / "attempts" / "1" / "events.jsonl",
            [{"type": "message_update", "usage": {"totalTokens": 777}}],
        )
        os.utime(
            foreign / "state" / "sessions" / "s" / "attempts" / "1" / "events.jsonl",
            None,
        )

        # The review has its own events and must keep them.
        _write_events(
            root
            / "operations"
            / f"task_{task.id}"
            / f"operation_{review.id}"
            / "state"
            / "sessions"
            / "session-review"
            / "attempts"
            / "1"
            / "events.jsonl",
            [
                {"type": "tool_execution_start", "toolCallId": "r", "toolName": "git diff"},
                {
                    "type": "message_update",
                    "usage": {"totalTokens": 999, "cost": {"total": 0.2}},
                },
            ],
        )

        snapshot = build_execution_monitor(database, root)
    finally:
        database.dispose()

    entries = {
        item["operation_id"]: item
        for item in snapshot["running"]
        if item["entry_type"] == "operation"
    }
    assert entries[linked.id]["tokens"]["totalTokens"] == 111
    assert entries[linked.id]["tools_used"] == 2
    assert entries[linked.id]["current_tool"] == "analyze"
    assert entries[missing.id]["session_available"] is False
    assert entries[missing.id]["tokens"] is None
    assert entries[missing.id]["tools_used"] is None
    assert entries[review.id]["tokens"]["totalTokens"] == 999
    assert entries[review.id]["tools_used"] == 1
    assert entries[review.id]["current_tool"] == "git diff"


def test_shared_analyzer_state_links_each_operation_to_its_own_session(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A shared analyzer state_dir must not leak a completed session.

    Both analyzer exports declare the same ``state_dir``. The newer operation
    has its export marker but has not created a session yet; it must degrade
    instead of borrowing the older operation's newest events.
    """
    database = Database.from_path(migrated_db)
    root = tmp_path / "artifacts"
    shared_state = tmp_path / "shared-analyzer-state"
    try:
        task = _running_task(database, "Shared analyzer state")
        operations = OperationRepository(database)
        older = operations.submit(
            kind="analysis", target_id=task.id, input={}
        ).operation
        newer = operations.submit(
            kind="analysis", target_id=task.id, input={}
        ).operation
        claimed = [operations.claim_next(worker_id="w") for _ in range(2)]
        assert {item.id for item in claimed if item is not None} == {
            older.id,
            newer.id,
        }

        older_export = root / "operations" / "analysis" / "older-export-uuid"
        older_run = older_export / "runs" / "20260101-old-run"
        older_run.mkdir(parents=True)
        (older_run / "task.txt").write_text(
            f"turn_id: analysis:{older.id}\n", encoding="utf-8"
        )
        (older_export / "runner_role.json").write_text(
            json.dumps({"state_dir": str(shared_state)}), encoding="utf-8"
        )

        newer_export = root / "operations" / "analysis" / "newer-export-uuid"
        newer_run = newer_export / "runs" / "20260101-new-run"
        newer_run.mkdir(parents=True)
        (newer_run / "task.txt").write_text(
            f"turn_id: analysis:{newer.id}\n", encoding="utf-8"
        )
        (newer_export / "runner_role.json").write_text(
            json.dumps({"state_dir": str(shared_state)}), encoding="utf-8"
        )

        _write_linked_session(
            shared_state,
            "older-session",
            older_export / "runner_role.json",
            f"analysis:{older.id}",
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "old",
                    "toolName": "old-analysis",
                },
                {
                    "type": "message_update",
                    "usage": {"totalTokens": 222, "cost": {"total": 0.02}},
                },
            ],
        )

        # A contradictory session has the newer turn marker but a config path
        # for the older export. Both identities must agree, so neither
        # operation may claim it.
        _write_linked_session(
            shared_state,
            "poison-session",
            older_export / "runner_role.json",
            f"analysis:{newer.id}",
            [{"type": "message_update", "usage": {"totalTokens": 444}}],
        )

        before = build_execution_monitor(database, root)
        before_entries = {
            item["operation_id"]: item
            for item in before["running"]
            if item["entry_type"] == "operation"
        }
        assert before_entries[older.id]["tokens"]["totalTokens"] == 222
        assert before_entries[newer.id]["session_available"] is False
        assert before_entries[newer.id]["tokens"] is None
        assert before_entries[newer.id]["current_tool"] is None

        # Once the newer operation writes its own session, only that session
        # is used. The older session remains present and must stay ignored.
        _write_linked_session(
            shared_state,
            "newer-session",
            newer_export / "runner_role.json",
            f"analysis:{newer.id}",
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "new",
                    "toolName": "new-analysis",
                },
                {"type": "message_update", "usage": {"totalTokens": 333}},
            ],
        )
        after = build_execution_monitor(database, root)
    finally:
        database.dispose()

    after_entries = {
        item["operation_id"]: item
        for item in after["running"]
        if item["entry_type"] == "operation"
    }
    assert after_entries[older.id]["tokens"]["totalTokens"] == 222
    assert after_entries[newer.id]["tokens"]["totalTokens"] == 333
    assert after_entries[newer.id]["current_tool"] == "new-analysis"


def test_newest_event_file_is_found_beyond_sixty_four_history_files(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    root = tmp_path / "artifacts"
    try:
        task = _running_task(database, "Long history")
        execution = root / "tasks" / f"task_{task.id}_history"
        now = time.time()
        for index in range(70):
            path = _write_events(
                execution / "history" / f"{index:03d}" / "events.jsonl",
                [
                    {"type": "tool_execution_start", "toolCallId": "old", "toolName": "old"},
                    {"type": "message_update", "usage": {"totalTokens": index}},
                ],
            )
            os.utime(path, (now - 1000, now - 1000))
        current = _write_events(
            execution / "zz-current" / "events.jsonl",
            [
                {"type": "tool_execution_start", "toolCallId": "new", "toolName": "new"},
                {"type": "message_update", "usage": {"totalTokens": 4242}},
            ],
        )
        os.utime(current, (now, now))

        snapshot = build_execution_monitor(database, root)
    finally:
        database.dispose()

    [entry] = snapshot["running"]
    assert entry["tokens"]["totalTokens"] == 4242
    assert entry["current_tool"] == "new"


def test_activity_age_is_timezone_independent(tmp_path: pathlib.Path) -> None:
    path = _write_events(
        tmp_path / "events.jsonl",
        [{"type": "message_update", "usage": {"totalTokens": 1}}],
    )
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    now_ts = now.replace(tzinfo=dt.timezone.utc).timestamp()
    os.utime(path, (now_ts - 3, now_ts - 3))

    previous_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Argentina/Buenos_Aires"
        time.tzset()
        summary = summarize_events(path, now)
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()

    assert summary["last_activity_sec"] is not None
    assert 0 <= summary["last_activity_sec"] <= 5
