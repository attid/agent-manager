"""Integration tests for the standalone review operation.

The real :class:`OperationWorker`, :class:`OperationHandler` registration and
the shared :class:`ReviewExecutor` are exercised against the deterministic
fake Pi Runner child process. No model or network is involved. The tests prove
that a completed task is actually reviewed without rerunning the developer and
that a reviewer failure or malformed report fails the operation.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

from kuzya.operation_handlers import (
    register_default_operation_handlers,
    review_operation_handler,
)
from kuzya.operation_worker import (
    OPERATION_KIND_REVIEW,
    OperationInstanceContext,
    OperationWorker,
)
from kuzya.runtime_defaults import RuntimeDefaultsResolver
from kuzya.storage import (
    AgentProfileRepository,
    Database,
    OperationRepository,
    TaskRepository,
)


@pytest.fixture
def fake_runner_argv() -> list[str]:
    runner_script = Path(__file__).parent / "fake_pi_runner.py"
    return [sys.executable, str(runner_script)]


def _seed_reviewer(
    database: Database,
    *,
    summary_text: str,
    mode: str = "success",
    extra: dict[str, Any] | None = None,
) -> None:
    config: dict[str, Any] = {
        "mode": mode,
        "summary_text": summary_text,
        "provider": "test-provider",
        "model": "test-model",
    }
    config.update(extra or {})
    AgentProfileRepository(database).create(
        agent_id="system-code-reviewer",
        name="System Code Reviewer",
        role="reviewer",
        system_prompt="Independent read-only reviewer.",
        runner_config=config,
    )


def _seed_completed_task(database: Database, workspace: Path) -> str:
    repository = TaskRepository(database)
    task = repository.create(
        title="Fix pagination",
        description="Fix the pagination bug.",
        status="ready_to_commit",
        workspace=str(workspace),
        metadata={
            "execution_status": "completed",
            "task_type": "coding",
            "changed_files": ["src/app.py"],
            "summary": "Implemented the fix.",
        },
    )
    return task.id


def _worker(
    database: Database,
    tmp_path: Path,
    fake_runner_argv: list[str],
    workspace: Path,
) -> OperationWorker:
    instance = OperationInstanceContext(
        database=database,
        artifact_root=tmp_path / "operations",
        runner_argv_prefix=tuple(fake_runner_argv),
        default_workspace=workspace,
        runtime_defaults=RuntimeDefaultsResolver(
            global_settings_path=tmp_path / "absent_settings.json"
        ),
    )
    return OperationWorker(database, instance=instance)


def test_registers_review_handler_at_default_seam(
    migrated_db: Path, tmp_path: Path, fake_runner_argv: list[str]
) -> None:
    database = Database.from_path(migrated_db)
    try:
        worker = _worker(database, tmp_path, fake_runner_argv, tmp_path)
        register_default_operation_handlers(worker)
        assert OPERATION_KIND_REVIEW in worker.registered_kinds()
        assert review_operation_handler is not None
    finally:
        database.dispose()


def test_completed_task_review_is_approved_without_rerunning_developer(
    migrated_db: Path, tmp_path: Path, fake_runner_argv: list[str]
) -> None:
    database = Database.from_path(migrated_db)
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        developer_log = tmp_path / "developer_argv.json"
        AgentProfileRepository(database).create(
            agent_id="kuzya-developer",
            name="Developer",
            role="developer",
            system_prompt="Developer.",
            runner_config={"argv_log": str(developer_log)},
        )
        task_id = _seed_completed_task(database, workspace)
        _seed_reviewer(database, summary_text='{"verdict": "approved", "findings": []}')

        operations = OperationRepository(database)
        submitted = operations.submit(
            kind=OPERATION_KIND_REVIEW,
            target_id=task_id,
            input={"instructions": "Check edge cases"},
        ).operation
        operation_id = submitted.id

        worker = _worker(database, tmp_path, fake_runner_argv, workspace)
        register_default_operation_handlers(worker)
        outcome = worker.step()

        assert outcome is not None
        assert outcome.status == "succeeded", outcome.error
        stored = operations.get(operation_id)
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.result is not None
        assert stored.result["verdict"] == "approved"
        assert stored.result["approved"] is True
        assert stored.result["reviewer_agent_id"] == "system-code-reviewer"
        assert Path(stored.result["report_path"]).is_file()
        assert Path(stored.result["metrics_path"]).is_file()

        # The developer profile was never invoked: the standalone review is not
        # a developer rerun gated on execution_status.
        assert not developer_log.exists()

        # The completed execution status is preserved.
        task = TaskRepository(database).get(task_id)
        assert task is not None
        assert task.meta["execution_status"] == "completed"
        assert task.meta["review"]["verdict"] == "approved"
    finally:
        database.dispose()


def test_changes_requested_review_links_findings(
    migrated_db: Path, tmp_path: Path, fake_runner_argv: list[str]
) -> None:
    database = Database.from_path(migrated_db)
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        task_id = _seed_completed_task(database, workspace)
        _seed_reviewer(
            database,
            summary_text=(
                '{"verdict": "changes_requested", "findings": [{"severity": "P1", "file": "src/app.py", "line": 9, "message": "src/app.py:9 — missing null check"}]}'
            ),
        )

        operations = OperationRepository(database)
        submitted = operations.submit(
            kind=OPERATION_KIND_REVIEW, target_id=task_id
        ).operation

        worker = _worker(database, tmp_path, fake_runner_argv, workspace)
        register_default_operation_handlers(worker)
        outcome = worker.step()

        assert outcome is not None and outcome.status == "succeeded"
        result = operations.get(submitted.id).result  # type: ignore[union-attr]
        assert result is not None
        assert result["verdict"] == "changes_requested"
        assert result["approved"] is False
        findings = result["findings"]
        assert findings[0]["file"] == "src/app.py"
        assert findings[0]["line"] == 9
        # The result is still an operation success: the verdict is domain data.
        assert operations.get(submitted.id).status == "succeeded"  # type: ignore[union-attr]
    finally:
        database.dispose()


def test_review_role_is_read_only_and_models_propagate(
    migrated_db: Path, tmp_path: Path, fake_runner_argv: list[str]
) -> None:
    database = Database.from_path(migrated_db)
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        task_id = _seed_completed_task(database, workspace)
        probe = tmp_path / "selection.json"
        _seed_reviewer(
            database,
            summary_text='{"verdict": "approved", "findings": []}',
            extra={
                "selection_probe": str(probe),
                "tools": ["read", "write", "bash"],
                "allow_git_commit": True,
                "write": True,
            },
        )

        operations = OperationRepository(database)
        submitted = operations.submit(
            kind=OPERATION_KIND_REVIEW, target_id=task_id
        ).operation

        worker = _worker(database, tmp_path, fake_runner_argv, workspace)
        register_default_operation_handlers(worker)
        outcome = worker.step()
        assert outcome is not None and outcome.status == "succeeded", outcome.error

        selection = json.loads(probe.read_text(encoding="utf-8"))
        assert selection == {
            "provider": "test-provider",
            "model": "test-model",
            "pi_auth_provider": "test-provider",
            "models_file": None,
        }

        role_path = (
            tmp_path
            / "operations"
            / f"task_{task_id}"
            / f"operation_{submitted.id}"
            / "role_r1.json"
        )
        role = json.loads(role_path.read_text(encoding="utf-8"))
        assert role["project_read_only"] is True
        assert role["read_only"] is True
        assert role["tools"] == ["read", "bash"]
        assert "allow_git_commit" not in role
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("mode", "summary_text", "reason"),
    [
        ("nonzero", '{"verdict": "approved", "findings": []}', "runner_nonzero_exit"),
        ("failed_status", '{"verdict": "approved", "findings": []}', "runner_status_not_completed"),
        ("malformed", "", "malformed_runner_output"),
        ("empty_summary", "", "empty_report"),
    ],
)
def test_review_failures_fail_the_operation_and_persist_diagnostics(
    migrated_db: Path,
    tmp_path: Path,
    fake_runner_argv: list[str],
    mode: str,
    summary_text: str,
    reason: str,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        task_id = _seed_completed_task(database, workspace)
        _seed_reviewer(database, summary_text=summary_text, mode=mode)

        operations = OperationRepository(database)
        submitted = operations.submit(
            kind=OPERATION_KIND_REVIEW, target_id=task_id
        ).operation

        worker = _worker(database, tmp_path, fake_runner_argv, workspace)
        register_default_operation_handlers(worker)
        outcome = worker.step()

        assert outcome is not None
        assert outcome.status == "failed"
        stored = operations.get(submitted.id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.result is None
        assert "review failed" in (stored.error or "")
        assert reason in (stored.error or "")

        # Failure diagnostics are durable on disk and in task metadata.
        diagnostics_path = (
            tmp_path
            / "operations"
            / f"task_{task_id}"
            / f"operation_{submitted.id}"
            / "review"
            / "round_1_failure.json"
        )
        assert diagnostics_path.is_file()
        diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
        assert diagnostics["reason"] == reason

        task = TaskRepository(database).get(task_id)
        assert task is not None
        assert task.meta["review"]["verdict"] is None
        assert task.meta["review"]["error"]
    finally:
        database.dispose()


def test_absent_verdict_never_approves(
    migrated_db: Path, tmp_path: Path, fake_runner_argv: list[str]
) -> None:
    database = Database.from_path(migrated_db)
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        task_id = _seed_completed_task(database, workspace)
        _seed_reviewer(
            database, summary_text="Everything looks fine, no P1 found.\n"
        )

        operations = OperationRepository(database)
        submitted = operations.submit(
            kind=OPERATION_KIND_REVIEW, target_id=task_id
        ).operation

        worker = _worker(database, tmp_path, fake_runner_argv, workspace)
        register_default_operation_handlers(worker)
        outcome = worker.step()

        assert outcome is not None and outcome.status == "failed"
        stored = operations.get(submitted.id)
        assert stored is not None
        assert stored.result is None
        assert "invalid_report" in (stored.error or "")
    finally:
        database.dispose()
