"""Operational read endpoints added for investigation without direct DB access.

Each test uses a migrated temporary file database. Review and session data is
written to temporary artifact/state trees shaped like the real workers, so the
endpoints exercise the same reader code as production without launching a
model.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib

from fastapi.testclient import TestClient

from kuzya.agent_sessions import list_agent_sessions
from kuzya.api import create_app
from kuzya.permissions import ExecutionPermissions, resolve_requirement
from kuzya.review_history import list_review_rounds
from kuzya.storage import (
    AgentProfileRepository,
    ConversationRepository,
    ConversationRuntime,
    Database,
    OperationRepository,
    TaskRepository,
)


def _app(db_path: pathlib.Path, artifact_root: pathlib.Path | None = None):
    return create_app(
        f"sqlite:///{db_path}",
        artifact_root=artifact_root,
    )


# -- operations listing ---------------------------------------------------


def test_operations_listing_supports_status_kind_target_limit_offset(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        review = client.post(
            "/operations",
            json={"kind": "review", "target_id": "task-a"},
        )
        analysis_a = client.post(
            "/operations",
            json={"kind": "analysis", "target_id": "task-a"},
        )
        analysis_b = client.post(
            "/operations",
            json={"kind": "analysis", "target_id": "task-b"},
        )
        assert review.status_code == 202
        assert analysis_a.status_code == 202
        assert analysis_b.status_code == 202
        review_id = review.json()["id"]
        analysis_ids = [analysis_a.json()["id"], analysis_b.json()["id"]]

        # One operation leaves the queue so the status filter has two states.
        database = Database.from_path(migrated_db)
        try:
            claimed = OperationRepository(database).claim_next(
                worker_id="listing-test"
            )
            assert claimed is not None
            assert claimed.id == review_id
        finally:
            database.dispose()

        running = client.get("/operations", params={"status": "running"})
        pending = client.get("/operations", params={"status": "pending"})
        by_kind = client.get("/operations", params={"kind": "analysis"})
        by_target = client.get("/operations", params={"target_id": "task-b"})
        by_both = client.get(
            "/operations",
            params={"kind": "analysis", "target_id": "task-a"},
        )
        paged = client.get(
            "/operations",
            params={"kind": "analysis", "limit": 1, "offset": 1},
        )

    assert running.status_code == 200
    assert [item["id"] for item in running.json()] == [review_id]
    assert [item["id"] for item in pending.json()] == analysis_ids
    assert [item["id"] for item in by_kind.json()] == analysis_ids
    assert [item["id"] for item in by_target.json()] == [analysis_ids[1]]
    assert [item["id"] for item in by_both.json()] == [analysis_ids[0]]
    assert [item["id"] for item in paged.json()] == [analysis_ids[1]]


# -- review rounds --------------------------------------------------------


def _write_round(
    directory: pathlib.Path,
    number: int,
    *,
    verdict: str,
    findings: list[dict],
    report: str,
    timestamp: str,
    task_id: str | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "round": number,
        "verdict": verdict.upper(),
        "domain_verdict": verdict,
        "findings": findings,
        "timestamp": timestamp,
    }
    if task_id is not None:
        payload["task_id"] = task_id
    (directory / f"round_{number}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    (directory / f"round_{number}.md").write_text(report, encoding="utf-8")


def test_task_review_returns_rounds_from_all_artifact_layouts_in_order(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    with TestClient(_app(migrated_db, artifact_root)) as client:
        task = client.post("/tasks", json={"title": "Reviewed task"}).json()
        task_id = task["id"]

        # Standalone review operation layout (the production worker path).
        _write_round(
            artifact_root
            / "operations"
            / f"task_{task_id}"
            / "operation_manual"
            / "review",
            1,
            verdict="changes_requested",
            findings=[
                {
                    "severity": "P1",
                    "message": "missing regression test",
                    "file": "src/app.py",
                    "line": 12,
                    "evidence": "no test for the new branch",
                }
            ],
            report="# Round 1\n\nFix the test.\n",
            timestamp="2026-01-01T10:00:00+00:00",
            task_id=task_id,
        )
        # In-worker review cycle layout.
        _write_round(
            artifact_root / "tasks" / task_id / "review",
            2,
            verdict="approved",
            findings=[],
            report="# Round 2\n\nApproved.\n",
            timestamp="2026-01-01T11:00:00+00:00",
            task_id=task_id,
        )

        response = client.get(f"/tasks/{task_id}/review")
        by_number = client.get(f"/tasks/{task['number']}/review")
        missing = client.get("/tasks/does-not-exist/review")

    assert response.status_code == 200
    rounds = response.json()
    assert by_number.status_code == 200
    assert by_number.json() == rounds
    assert [item["round"] for item in rounds] == [1, 2]
    assert [item["verdict"] for item in rounds] == [
        "changes_requested",
        "approved",
    ]
    assert rounds[0]["findings"][0]["severity"] == "P1"
    assert rounds[0]["report"] == "# Round 1\n\nFix the test.\n"
    assert rounds[1]["findings"] == []
    assert rounds[0]["created_at"].startswith("2026-01-01T10:00:00")

    assert missing.status_code == 404


def test_review_reader_ignores_malformed_artifacts(
    tmp_path: pathlib.Path,
) -> None:
    review_dir = tmp_path / "tasks" / "task-1" / "review"
    review_dir.mkdir(parents=True)
    (review_dir / "round_1.json").write_text("{not json", encoding="utf-8")
    (review_dir / "round_2.json").write_text(
        json.dumps({"round": 2, "verdict": "APPROVED"}), encoding="utf-8"
    )

    rounds = list_review_rounds(tmp_path, "task-1")

    assert [item["round"] for item in rounds] == [2]
    assert rounds[0]["report"] == ""


def test_review_reader_accepts_embedded_report_and_created_at(
    tmp_path: pathlib.Path,
) -> None:
    review_dir = tmp_path / "tasks" / "task-1" / "review"
    review_dir.mkdir(parents=True)
    (review_dir / "round_3.json").write_text(
        json.dumps(
            {
                "round": 3,
                "verdict": "approved",
                "findings": [],
                "report": "embedded report text",
                "created_at": "2026-02-03T04:05:06Z",
            }
        ),
        encoding="utf-8",
    )

    [round_item] = list_review_rounds(tmp_path, "task-1")

    assert round_item["report"] == "embedded report text"
    assert round_item["created_at"].isoformat() == "2026-02-03T04:05:06"


def test_review_rounds_do_not_mix_prefixed_task_identifiers(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Task ``alpha`` must never read artifacts owned by ``task_alpha``.

    The ``task_`` prefix is ambiguous because it can be either a layout
    convention or another task's literal id. A restricted token for
    ``task:alpha`` proves that the prefixed directory and the old
    prefix-match fallback cannot leak the other task's findings or report.
    """
    artifact_root = tmp_path / "artifacts"
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        tasks.create(id="alpha", title="Alpha")
        tasks.create(id="task_alpha", title="Task alpha")
    finally:
        database.dispose()

    _write_round(
        artifact_root / "tasks" / "alpha" / "review",
        1,
        verdict="approved",
        findings=[{"severity": "P2", "message": "alpha-only finding"}],
        report="alpha report",
        timestamp="2026-01-01T10:00:00+00:00",
        task_id="alpha",
    )
    _write_round(
        artifact_root / "tasks" / "task_alpha" / "review",
        1,
        verdict="changes_requested",
        findings=[{"severity": "P1", "message": "secret task_alpha finding"}],
        report="secret task_alpha report",
        timestamp="2026-01-01T11:00:00+00:00",
        task_id="task_alpha",
    )
    _write_round(
        artifact_root / "tasks" / "task_alpha" / "review",
        5,
        verdict="approved",
        findings=[],
        report="legacy no identity report",
        timestamp="2026-01-01T12:30:00+00:00",
    )
    # Same collision on the standalone-operation layout.
    _write_round(
        artifact_root
        / "operations"
        / "task_alpha"
        / "operation_other"
        / "review",
        2,
        verdict="approved",
        findings=[],
        report="secret operation report",
        timestamp="2026-01-01T12:00:00+00:00",
        task_id="task_alpha",
    )
    # The removed prefix-match fallback must not resurface.
    _write_round(
        artifact_root / "tasks" / "task_alpha_deadbeef" / "review",
        9,
        verdict="approved",
        findings=[],
        report="secret prefix-match report",
        timestamp="2026-01-01T13:00:00+00:00",
        task_id="task_alpha",
    )

    permissions = ExecutionPermissions()
    alpha_grant = permissions.issue(
        agent_id="alpha-reader",
        actions=["tasks.read"],
        resources=["task:alpha"],
    )
    other_grant = permissions.issue(
        agent_id="task-alpha-reader",
        actions=["tasks.read"],
        resources=["task:task_alpha"],
    )
    app = create_app(
        f"sqlite:///{migrated_db}",
        permissions=permissions,
        artifact_root=artifact_root,
    )
    with TestClient(app) as client:
        alpha = client.get(
            "/tasks/alpha/review",
            headers={"Authorization": f"Bearer {alpha_grant.token}"},
        )
        foreign = client.get(
            "/tasks/task_alpha/review",
            headers={"Authorization": f"Bearer {alpha_grant.token}"},
        )
        other = client.get(
            "/tasks/task_alpha/review",
            headers={"Authorization": f"Bearer {other_grant.token}"},
        )

    assert alpha.status_code == 200
    alpha_rounds = alpha.json()
    assert [item["round"] for item in alpha_rounds] == [1]
    assert alpha_rounds[0]["report"] == "alpha report"
    assert alpha_rounds[0]["findings"][0]["message"] == "alpha-only finding"
    assert "secret" not in json.dumps(alpha_rounds)

    assert foreign.status_code == 403

    assert other.status_code == 200
    other_rounds = other.json()
    assert [item["round"] for item in other_rounds] == [1, 2, 5]
    assert {item["report"] for item in other_rounds} == {
        "secret task_alpha report",
        "secret operation report",
        "legacy no identity report",
    }


def test_review_reader_survives_out_of_range_numeric_timestamps(
    tmp_path: pathlib.Path,
) -> None:
    review_dir = tmp_path / "tasks" / "task-1" / "review"
    review_dir.mkdir(parents=True)
    for number, timestamp in (
        (1, 1e100),
        (2, float("nan")),
        (3, float("inf")),
    ):
        (review_dir / f"round_{number}.json").write_text(
            json.dumps(
                {
                    "round": number,
                    "verdict": "approved",
                    "findings": [],
                    "timestamp": timestamp,
                }
            ),
            encoding="utf-8",
        )
    (review_dir / "round_4.json").write_text(
        json.dumps(
            {
                "round": 4,
                "verdict": "approved",
                "findings": [],
                "timestamp": "2026-03-04T05:06:07Z",
            }
        ),
        encoding="utf-8",
    )

    rounds = list_review_rounds(tmp_path, "task-1")

    assert [item["round"] for item in rounds] == [1, 2, 3, 4]
    assert all(isinstance(item["created_at"], dt.datetime) for item in rounds)
    assert rounds[3]["created_at"].isoformat() == "2026-03-04T05:06:07"


# -- conversations filter -------------------------------------------------


def test_conversations_agent_filter_matches_runtime_and_metadata(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversations.create_conversation(
            id="meta-conv", metadata={"talkable_agent": "direct-agent"}
        )
        conversations.create_conversation(
            id="other-conv", metadata={"talkable_agent": "someone-else"}
        )
        conversations.create_conversation(id="runtime-conv")
        AgentProfileRepository(database).create(
            agent_id="runtime-agent",
            name="Runtime Agent",
            role="worker",
            system_prompt="You are helpful.",
        )
        with database.session() as session:
            session.add(
                ConversationRuntime(
                    conversation_id="runtime-conv",
                    agent_id="runtime-agent",
                    profile_revision=1,
                )
            )
    finally:
        database.dispose()

    with TestClient(_app(migrated_db)) as client:
        direct = client.get(
            "/conversations", params={"agent_id": "direct-agent"}
        )
        runtime = client.get(
            "/conversations", params={"agent_id": "runtime-agent"}
        )
        unknown = client.get(
            "/conversations", params={"agent_id": "nobody"}
        )
        unfiltered = client.get("/conversations")
        limited = client.get(
            "/conversations",
            params={"agent_id": "direct-agent", "limit": 1, "offset": 0},
        )

    assert direct.status_code == 200
    assert [item["id"] for item in direct.json()] == ["meta-conv"]
    assert [item["id"] for item in runtime.json()] == ["runtime-conv"]
    assert unknown.json() == []
    assert {item["id"] for item in unfiltered.json()} == {
        "meta-conv",
        "other-conv",
        "runtime-conv",
    }
    assert [item["id"] for item in limited.json()] == ["meta-conv"]


# -- agent Runner sessions ------------------------------------------------


def _touch(path: pathlib.Path, timestamp: float) -> None:
    os.utime(path, (timestamp, timestamp))


def test_agent_sessions_list_metadata_without_contents(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    state_dir = tmp_path / "state"
    database = Database.from_path(migrated_db)
    try:
        AgentProfileRepository(database).create(
            agent_id="session-agent",
            name="Session Agent",
            role="worker",
            system_prompt="You are helpful.",
            runner_config={"state_dir": str(state_dir)},
        )
    finally:
        database.dispose()

    now = dt.datetime.now().timestamp()
    old_session = state_dir / "sessions" / "session-old"
    for number in (1, 2):
        (old_session / "attempts" / str(number)).mkdir(parents=True)
    (old_session / "attempts" / "1" / "events.jsonl").write_text(
        '{"private": "model data"}\n', encoding="utf-8"
    )
    _touch(old_session / "attempts" / "1", now - 500)
    _touch(old_session / "attempts" / "2", now - 100)
    _touch(old_session, now - 50)

    new_session = state_dir / "sessions" / "session-new"
    (new_session / "attempts" / "1").mkdir(parents=True)
    _touch(new_session / "attempts" / "1", now - 20)
    _touch(new_session, now - 10)

    with TestClient(_app(migrated_db)) as client:
        response = client.get("/agents/session-agent/sessions")
        missing = client.get("/agents/does-not-exist/sessions")
        unknown_state_agent = client.post(
            "/agents",
            json={
                "agent_id": "no-state",
                "name": "No State",
                "role": "worker",
                "system_prompt": "You are helpful.",
            },
        )
        assert unknown_state_agent.status_code == 201
        empty = client.get("/agents/no-state/sessions")

    assert response.status_code == 200
    sessions = response.json()
    assert [item["session_id"] for item in sessions] == [
        "session-new",
        "session-old",
    ]
    old = sessions[1]
    assert old["attempts_count"] == 2
    assert old["created_at"]
    assert old["last_activity"]
    # Only metadata fields are exposed; a session's events file never leaks.
    assert set(old) == {
        "session_id",
        "created_at",
        "last_activity",
        "attempts_count",
    }
    assert "private" not in json.dumps(sessions)

    assert missing.status_code == 404
    assert empty.status_code == 200
    assert empty.json() == []


def test_agent_sessions_reject_relative_state_dir_without_reading(
    tmp_path: pathlib.Path,
) -> None:
    # A relative path is not trustworthy after expanduser; the reader returns
    # an empty list instead of resolving it against the server cwd.
    (tmp_path / "relative-state" / "sessions" / "s").mkdir(parents=True)
    assert list_agent_sessions("relative-state") == []


# -- monitor verbose ------------------------------------------------------


def test_monitor_verbose_adds_operation_input_and_changed_files(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    from .test_monitor import _seed_running_work

    artifact_root = tmp_path / "artifacts"
    database, task_id, _operation_id, _task_events, _operation_events = (
        _seed_running_work(migrated_db, artifact_root)
    )
    try:
        TaskRepository(database).update(
            task_id, metadata={"changed_files": ["src/a.py", "src/b.py"]}
        )
        operations = OperationRepository(database)
        submitted = operations.submit(
            kind="review",
            target_id=task_id,
            input={"instructions": "focus on auth"},
        ).operation
        claimed = operations.claim_next(worker_id="verbose-test")
        # The seed already claimed the first operation; this claim is ours.
        assert claimed is not None and claimed.id == submitted.id
    finally:
        database.dispose()

    with TestClient(_app(migrated_db, artifact_root)) as client:
        verbose = client.get("/monitor/executions", params={"verbose": "true"})
        plain = client.get("/monitor/executions")

    assert verbose.status_code == 200
    verbose_entries = verbose.json()["running"]
    task_entry = next(
        item for item in verbose_entries if item["entry_type"] == "task"
    )
    review_entry = next(
        item
        for item in verbose_entries
        if item.get("operation_id") == submitted.id
    )
    assert task_entry["changed_files"] == ["src/a.py", "src/b.py"]
    assert task_entry["changed_files_count"] == 2
    assert review_entry["input"] == {"instructions": "focus on auth"}

    plain_entries = plain.json()["running"]
    plain_task = next(
        item for item in plain_entries if item["entry_type"] == "task"
    )
    plain_review = next(
        item for item in plain_entries if item.get("operation_id") == submitted.id
    )
    assert "changed_files" not in plain_task
    assert "changed_files_count" not in plain_task
    assert "input" not in plain_review


# -- permission parity ----------------------------------------------------


def test_new_operational_routes_require_their_read_action() -> None:
    review = resolve_requirement("GET", "/tasks/task-1/review")
    assert review is not None
    assert review.action == "tasks.read"
    assert review.resource == "task:task-1"

    sessions = resolve_requirement("GET", "/agents/agent-1/sessions")
    assert sessions is not None
    assert sessions.action == "agents.read"
    assert sessions.resource == "agent:agent-1"

    conversations = resolve_requirement("GET", "/conversations")
    assert conversations is not None
    assert conversations.action == "conversations.read"
