"""Unit tests for GitEngine: safe commit, rollback, and CI monitoring."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_manager.git_engine import GitEngine, GitEngineResult


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a temporary initialized git repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=repo, check=True)

    # Initial file
    init_file = repo / "README.md"
    init_file.write_text("# Test Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


def test_commit_local_explicit_files(temp_git_repo: Path) -> None:
    engine = GitEngine(default_workspace=temp_git_repo)

    # Modify an existing file and create a new file
    (temp_git_repo / "README.md").write_text("# Updated Repo\n")
    new_file = temp_git_repo / "src" / "feature.py"
    new_file.parent.mkdir(parents=True)
    new_file.write_text("print('feature')\n")

    # Untracked sensitive file that must NOT be staged
    sensitive_file = temp_git_repo / "secret.env"
    sensitive_file.write_text("SECRET=12345\n")

    res = engine.commit_local(
        workspace=temp_git_repo,
        task_id="task-42",
        title="Add new feature",
        summary="Implemented feature and updated README",
        changed_files=["README.md", "src/feature.py"],
    )

    assert res.success is True
    assert res.commit_hash is not None
    assert len(res.commit_hash) == 40
    assert res.action == "commit_local"

    # Verify commit log
    log = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=temp_git_repo, capture_output=True, text=True).stdout
    assert "feat(#task-42): Add new feature" in log
    assert "Implemented feature and updated README" in log

    # Verify sensitive file was NOT committed
    status = subprocess.run(["git", "status", "--porcelain"], cwd=temp_git_repo, capture_output=True, text=True).stdout
    assert "?? secret.env" in status


def test_rollback_discards_changes(temp_git_repo: Path) -> None:
    engine = GitEngine(default_workspace=temp_git_repo)

    # Modify README and add new untracked file
    (temp_git_repo / "README.md").write_text("# Corrupted Content\n")
    new_file = temp_git_repo / "temp_experiment.py"
    new_file.write_text("# Temp\n")

    res = engine.rollback(
        workspace=temp_git_repo,
        changed_files=["README.md", "temp_experiment.py"],
    )

    assert res.success is True
    assert (temp_git_repo / "README.md").read_text() == "# Test Repo\n"
    assert not new_file.exists()


def test_commit_and_push_with_ci_success(temp_git_repo: Path) -> None:
    engine = GitEngine(default_workspace=temp_git_repo)
    (temp_git_repo / "README.md").write_text("# Pushed\n")

    mock_run = MagicMock()
    # 1. git push
    # 2. gh run list
    # 3. gh run watch
    def fake_subprocess_run(args, **kwargs):
        if "push" in args:
            return MagicMock(returncode=0, stdout="pushed", stderr="")
        elif "list" in args and "gh" in args:
            return MagicMock(returncode=0, stdout='[{"databaseId": 998877, "status": "completed", "conclusion": "success"}]', stderr="")
        elif "watch" in args and "gh" in args:
            return MagicMock(returncode=0, stdout="Run succeeded", stderr="")
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(args, **kwargs)

    with patch.object(engine, "_run_cmd", side_effect=fake_subprocess_run):
        res = engine.commit_and_push(
            workspace=temp_git_repo,
            task_id="task-99",
            title="Push test",
            changed_files=["README.md"],
            watch_ci=True,
        )

    assert res.success is True
    assert res.ci_status == "success"
    assert "GitHub Actions успешно пройдены" in res.message


def test_commit_and_push_ci_failure(temp_git_repo: Path) -> None:
    engine = GitEngine(default_workspace=temp_git_repo)
    (temp_git_repo / "README.md").write_text("# Broken\n")

    def fake_subprocess_run(args, **kwargs):
        if "push" in args:
            return MagicMock(returncode=0, stdout="pushed", stderr="")
        elif "list" in args and "gh" in args:
            return MagicMock(returncode=0, stdout='[{"databaseId": 12345, "status": "completed", "conclusion": "failure"}]', stderr="")
        elif "watch" in args and "gh" in args:
            return MagicMock(returncode=1, stdout="", stderr="Watch failed")
        elif "view" in args and "--log-failed" in args:
            return MagicMock(returncode=0, stdout="Error: tests failed at line 42", stderr="")
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(args, **kwargs)

    with patch.object(engine, "_run_cmd", side_effect=fake_subprocess_run):
        res = engine.commit_and_push(
            workspace=temp_git_repo,
            task_id="task-100",
            title="Push with failure",
            changed_files=["README.md"],
            watch_ci=True,
        )

    assert res.success is False
    assert res.ci_status == "failure"
    assert "tests failed at line 42" in (res.ci_logs or "")
