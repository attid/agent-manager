"""Tests for TaskExecutionWorker and TelegramNotifier."""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_manager.storage import (
    AgentProfileRepository,
    Database,
    TaskRepository,
)
from agent_manager.task_worker import TaskExecutionWorker, TaskOutcome
from agent_manager.telegram.notifier import (
    TelegramNotifier,
    build_task_outcome_keyboard,
    format_task_outcome_message,
)


def sync_test(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapper


@pytest.fixture
def fake_runner_argv() -> list[str]:
    runner_script = Path(__file__).parent / "fake_pi_runner.py"
    return [sys.executable, str(runner_script)]


@pytest.fixture
def setup_developer_agent(migrated_db: Path) -> tuple[Database, str]:
    db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(db)
    agent_id = "agent-manager-developer"
    agent_repo.create(
        agent_id=agent_id,
        name="Developer",
        role="developer",
        system_prompt="You are a developer agent.",
        runner_config={"mode": "success", "summary_text": "Code updated."},
    )
    return db, agent_id


def test_task_execution_worker_success(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Fix bug in auth module",
        description="Fix the auth token validation logic.",
        status="in_progress",
        workspace="test_ws",
    )

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    artifact_root = tmp_path / "artifacts"

    agent_repo = AgentProfileRepository(test_db)
    agent_repo.update(
        agent_id=agent_id,
        expected_revision=1,
        system_prompt="Developer agent prompt",
        runner_config={
            "mode": "success",
            "summary_text": "Successfully fixed auth validation in token.py",
            "changed_files": ["src/auth/token.py"],
        },
    )

    notifications: list[tuple[Any, TaskOutcome]] = []

    def on_finished(updated_task: Any, outcome: TaskOutcome) -> None:
        notifications.append((updated_task, outcome))

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=artifact_root,
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        workspace_map={"test_ws": str(workspace_dir)},
        on_task_finished=on_finished,
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "completed"
    assert outcome.task_id == task.id
    assert "Successfully fixed auth" in (outcome.summary or "")
    assert outcome.changed_files == ["src/auth/token.py"]
    assert outcome.duration_ms > 0

    reloaded = task_repo.get(task.id)
    assert reloaded.status == "done"
    assert reloaded.meta["summary"] == outcome.summary
    assert reloaded.meta["changed_files"] == ["src/auth/token.py"]
    assert reloaded.meta["execution_finished_at"] is not None

    assert len(notifications) == 1
    assert notifications[0][0].id == task.id
    assert notifications[0][1].status == "completed"


def test_task_execution_worker_no_in_progress(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task_repo.create(title="Incoming task", status="incoming")

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
    )

    outcome = worker.step()
    assert outcome is None


def test_task_execution_worker_runner_nonzero_exit(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Failing task",
        status="in_progress",
    )

    agent_repo = AgentProfileRepository(test_db)
    agent_repo.update(
        agent_id=agent_id,
        expected_revision=1,
        system_prompt="Developer agent prompt",
        runner_config={"mode": "nonzero", "stderr_text": "Runner crash detected"},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "failed"
    assert "Runner process exited with code" in (outcome.error or "")
    assert "Runner crash detected" in (outcome.error or "")

    reloaded = task_repo.get(task.id)
    assert reloaded.status == "failed"
    assert reloaded.meta["execution_finished_at"] is not None


def test_task_execution_worker_runner_failed_status(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Task failing internally",
        status="in_progress",
    )

    agent_repo = AgentProfileRepository(test_db)
    agent_repo.update(
        agent_id=agent_id,
        expected_revision=1,
        system_prompt="Developer agent prompt",
        runner_config={"mode": "failed_status"},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "failed"
    assert "Runner reported status 'failed'" in (outcome.error or "")

    reloaded = task_repo.get(task.id)
    assert reloaded.status == "failed"


def test_task_execution_worker_missing_agent_profile(
    migrated_db: Path,
    tmp_path: Path,
    fake_runner_argv: list[str],
) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Task with unknown agent",
        status="in_progress",
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="non-existent-agent",
        runner_argv_prefix=fake_runner_argv,
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "failed"
    assert "Agent profile not found in registry" in (outcome.error or "")

    reloaded = task_repo.get(task.id)
    assert reloaded.status == "failed"


def test_task_execution_worker_callback_exception_does_not_crash(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Callback error test",
        status="in_progress",
    )

    def crashing_callback(t: Any, o: Any) -> None:
        raise ValueError("Explosion in notification callback!")

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
        on_task_finished=crashing_callback,
    )

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "completed"


def test_task_execution_worker_lifecycle(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    test_db, agent_id = setup_developer_agent
    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        poll_interval_seconds=0.05,
    )

    worker.start()
    assert worker._thread is not None
    assert worker._thread.is_alive()

    worker.stop(timeout=2.0)
    assert not worker._thread.is_alive()


# -- Telegram Notifier Tests --


def test_format_task_outcome_message_success(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Add <sanitized> HTML support",
        workspace="backend",
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="completed",
        summary="Added & tested <b>sanitized</b> output.",
        changed_files=["src/app.py", "tests/test_app.py"],
        duration_ms=4500.0,
    )

    msg = format_task_outcome_message(task, outcome)
    assert "✅" in msg
    assert f"Задача #{task.id} завершена!" in msg
    assert "&lt;sanitized&gt;" in msg
    assert "&lt;b&gt;sanitized&lt;/b&gt;" in msg
    assert "<code>src/app.py</code>" in msg
    assert "⏱ <b>Время:</b> 4.5с" in msg

    kb = build_task_outcome_keyboard(task.id)
    assert kb.inline_keyboard[0][0].callback_data == f"select_task:{task.id}"


def test_format_task_outcome_message_failure(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Deploy worker",
        workspace="ops",
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="failed",
        error="ConnectionRefusedError: Port <8080> unreachable",
        duration_ms=1200.0,
    )

    msg = format_task_outcome_message(task, outcome)
    assert "❌" in msg
    assert f"Задача #{task.id} завершилась с ошибкой!" in msg
    assert "&lt;8080&gt;" in msg
    assert "pre>" in msg


@sync_test
async def test_telegram_notifier_async_send(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Notification test task",
        metadata={"chat_id": 12345678, "user_id": 12345678},
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="completed",
        summary="Done successfully.",
    )

    bot_mock = AsyncMock()
    notifier = TelegramNotifier(bot=bot_mock)

    await notifier.notify_async(task, outcome)

    bot_mock.send_message.assert_awaited_once()
    kwargs = bot_mock.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 12345678
    assert "✅" in kwargs["text"]
    assert kwargs["parse_mode"] == "HTML"


@sync_test
async def test_telegram_notifier_fallback_to_default_chat_id(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Task with no chat_id",
        metadata={},
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="failed",
        error="Some error",
    )

    bot_mock = AsyncMock()
    notifier = TelegramNotifier(bot=bot_mock, default_chat_id=999888)

    await notifier.notify_async(task, outcome)

    bot_mock.send_message.assert_awaited_once()
    assert bot_mock.send_message.await_args.kwargs["chat_id"] == 999888


@sync_test
async def test_telegram_notifier_suppresses_bot_errors(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Task failing bot",
        metadata={"chat_id": 12345},
    )
    outcome = TaskOutcome(task_id=task.id, status="completed")

    bot_mock = AsyncMock()
    bot_mock.send_message.side_effect = RuntimeError("Telegram Network Timeout")

    notifier = TelegramNotifier(bot=bot_mock)
    # Should not raise exception
    await notifier.notify_async(task, outcome)
    bot_mock.send_message.assert_awaited_once()


def test_task_worker_skills_and_devops_git_commit(migrated_db: Path, tmp_path: Path) -> None:
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    agent_repo.create(
        agent_id="devops-agent",
        name="DevOps Agent",
        role="devops",
        system_prompt="Manage devops tasks.",
        runner_config={"tools": ["read", "bash"], "skills": ["portainer-operations"]},
        skills=[{"skill": "portainer-operations"}],
    )

    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Check container logs",
        status="in_progress",
        metadata={"task_type": "devops"},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="devops-agent",
        default_workspace=str(tmp_path),
        runner_argv_prefix=["true"],
    )

    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout='{"status": "completed", "changed_files": []}', stderr=""))
    with patch("subprocess.run", mock_run):
        outcome = worker.step()

    assert outcome is not None
    assert mock_run.called
    # Check that role.json was written with skills and allow_git_commit
    exec_dirs = list((tmp_path / "artifacts").glob(f"task_{task.id}_*"))
    assert len(exec_dirs) == 1
    role_content = json.loads((exec_dirs[0] / "role.json").read_text())
    assert role_content.get("skills") == ["portainer-operations"]
    assert role_content.get("allow_git_commit") is True


def test_task_worker_code_review_cycle_invoked(migrated_db: Path, tmp_path: Path) -> None:
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    agent_repo.create(
        agent_id="agent-manager-developer",
        name="Developer",
        role="developer",
        system_prompt="Coding prompt.",
        runner_config={"tools": ["read", "write"]},
    )

    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Fix pagination bug",
        status="in_progress",
        metadata={"task_type": "coding", "requires_review": True},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="agent-manager-developer",
        default_workspace=str(tmp_path),
        runner_argv_prefix=["true"],
    )

    dev_output = json.dumps({"status": "completed", "changed_files": ["src/bot.py"], "summary_path": str(tmp_path / "dev_sum.md")})
    (tmp_path / "dev_sum.md").write_text("Fixed pagination.")

    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=dev_output, stderr=""))
    with patch("subprocess.run", mock_run):
            with patch.object(worker, "_run_review_cycle") as mock_review:
                mock_review.return_value = {
                    "summary": "Fixed pagination.\n\n### 🔍 Code Review: ОДОБРЕНО ✅",
                    "changed_files": ["src/bot.py"],
                    "needs_user_input": False,
                }
                outcome = worker.step()

    assert outcome is not None
    assert mock_review.called
    assert "ОДОБРЕНО" in outcome.summary
    assert outcome.status == "ready_to_commit"
    reloaded = task_repo.get(task.id)
    assert reloaded.status == "ready_to_commit"


def test_format_task_outcome_message_ready_to_commit(migrated_db: Path) -> None:
    test_db = Database.from_path(migrated_db)
    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Implement commit confirmation",
        workspace="project_a",
        status="ready_to_commit",
    )
    outcome = TaskOutcome(
        task_id=task.id,
        status="ready_to_commit",
        summary="Code reviewed and passed all checks.",
        changed_files=["src/main.py", "src/utils.py"],
        duration_ms=3500.0,
    )

    msg = format_task_outcome_message(task, outcome)
    assert "🔍" in msg
    assert f"Задача #{task.id} ожидает подтверждения!" in msg
    assert "<code>src/main.py</code>" in msg
    assert "<code>src/utils.py</code>" in msg
    assert "Выберите действие ниже:" in msg

    kb = build_task_outcome_keyboard(task.id, status="ready_to_commit")
    # Row 1: commit_local, commit_push
    assert kb.inline_keyboard[0][0].text == "✅ Закоммитить"
    assert kb.inline_keyboard[0][0].callback_data == f"commit_local:{task.id}"
    assert kb.inline_keyboard[0][1].text == "🚀 Закоммитить и запушить"
    assert kb.inline_keyboard[0][1].callback_data == f"commit_push:{task.id}"
    # Row 2: rollback, discuss
    assert kb.inline_keyboard[1][0].text == "❌ Откатить"
    assert kb.inline_keyboard[1][0].callback_data == f"rollback:{task.id}"
    assert kb.inline_keyboard[1][1].callback_data == f"select_task:{task.id}"


def test_task_worker_run_review_cycle_metrics_and_reload(migrated_db: Path, tmp_path: Path) -> None:
    test_db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(test_db)
    agent_repo.create(
        agent_id="system-code-reviewer",
        name="System Reviewer",
        role="reviewer",
        system_prompt="Custom Reviewer Rules: Always check edge cases.",
        runner_config={"tools": ["read", "bash"]},
    )

    task_repo = TaskRepository(test_db)
    task = task_repo.create(
        title="Check review loop",
        status="in_progress",
        metadata={"task_type": "coding"},
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=tmp_path / "artifacts",
        agent_id="agent-manager-developer",
        default_workspace=str(tmp_path),
        runner_argv_prefix=["true"],
    )

    review_out_file = tmp_path / "review_out.md"
    review_out_file.write_text("Код проверен.\nВЕРДИКТ: ОДОБРЕНО\n")

    exec_dir = tmp_path / "exec"
    exec_dir.mkdir(parents=True)
    role_file = exec_dir / "role.json"
    role_file.write_text("{}")
    state_dir = exec_dir / "state"
    state_dir.mkdir()

    mock_proc = MagicMock(
        returncode=0,
        stdout=json.dumps({"status": "completed", "summary_path": str(review_out_file)}),
        stderr="",
    )

    with patch("subprocess.run", return_value=mock_proc):
        res = worker._run_review_cycle(
            task=task,
            project_dir=tmp_path,
            changed_files=["file1.py"],
            developer_summary="Implemented feature.",
            exec_dir=exec_dir,
            role_file=role_file,
            state_dir=state_dir,
            child_env={},
        )

    assert res["needs_user_input"] is False
    assert "ОДОБРЕНО" in res["summary"]

    # Verify review metrics persisted
    round_json = tmp_path / "artifacts" / str(task.id) / "review" / "round_1.json"
    assert round_json.is_file()
    data = json.loads(round_json.read_text())
    assert data["round"] == 1
    assert data["verdict"] == "APPROVED"
    assert data["approved"] is True
    assert "duration_ms" in data
