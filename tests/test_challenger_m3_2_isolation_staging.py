"""Adversarial challenge test suite for Milestone 3 (Challenger M3-2).

Empirical verification of:
1. Scoped Token Isolation:
   - Create Task 1 and Task 2.
   - Scoped token with `tasks.write` scoped to `task:1`.
   - Token successfully uploads attachment to Task 1 (HTTP 201 Created).
   - Same token is rejected with HTTP 403 Forbidden when uploading to Task 2.
   - Additional boundaries: numeric prefix isolation (task:1 vs task:10), reciprocal token
     isolation, read-only token rejection, collection route rejection.
2. Worker Inbox Staging:
   - TaskExecutionWorker.step() stages attachments into inbox_dir with chmod 0o444.
   - Re-running staging over pre-existing 0o444 file succeeds without PermissionError.
   - Missing attachment source file logs warning and completes without crashing worker.
   - Unlink OSError fallback to chmod 0o644 verified under retry.
   - Mixed attachments (direct path, prior task artifact, path traversal, missing) handled safely.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import stat
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from kuzya.api import create_app
from kuzya.permissions import (
    ExecutionGrant,
    ExecutionPermissions,
    actions_for_tools,
    resources_for_execution,
)
from kuzya.storage import (
    AgentProfileRepository,
    Database,
    TaskRepository,
)
from kuzya.task_worker import TaskExecutionWorker


@pytest.fixture
def fake_runner_argv() -> list[str]:
    runner_script = Path(__file__).parent / "fake_pi_runner.py"
    return [sys.executable, str(runner_script)]


@pytest.fixture
def setup_developer_agent(migrated_db: Path) -> tuple[Database, str]:
    db = Database.from_path(migrated_db)
    agent_repo = AgentProfileRepository(db)
    agent_id = "kuzya-developer"
    agent_repo.create(
        agent_id=agent_id,
        name="Developer",
        role="developer",
        system_prompt="You are a developer agent.",
        runner_config={"mode": "success", "summary_text": "Code updated."},
    )
    return db, agent_id


# ============================================================================
# Section 1: Empirical Tests for Scoped Token Isolation
# ============================================================================


def test_scoped_token_isolation_between_two_tasks(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Empirical test 1:
    - Create two tasks: Task 1 and Task 2.
    - Generate a scoped token with permission tasks.write scoped to task:1.
    - Upload to Task 1 succeeds (HTTP 201 Created).
    - Upload to Task 2 is REJECTED with HTTP 403 Forbidden.
    """
    artifact_root = tmp_path / "artifacts"
    permissions = ExecutionPermissions()
    app = create_app(
        f"sqlite:///{migrated_db}",
        api_tokens={"administrative-token"},
        permissions=permissions,
        artifact_root=artifact_root,
    )

    with TestClient(app) as client:
        # Create Task 1 and Task 2 using admin authority
        admin_headers = {"Authorization": "Bearer administrative-token"}
        r1 = client.post(
            "/tasks",
            json={"title": "Task 1: Primary Task", "workspace": "/tmp"},
            headers=admin_headers,
        )
        assert r1.status_code == 201
        task_1_id = r1.json()["id"]

        r2 = client.post(
            "/tasks",
            json={"title": "Task 2: Secondary Task", "workspace": "/tmp"},
            headers=admin_headers,
        )
        assert r2.status_code == 201
        task_2_id = r2.json()["id"]

        # Issue scoped token with tasks.write scoped strictly to task:1
        actions = actions_for_tools(["update_task"])
        assert "tasks.write" in actions
        grant_task_1 = permissions.issue(
            agent_id="developer-agent",
            actions=actions,
            resources=resources_for_execution(
                actions, task_id=task_1_id, agent_id="developer-agent"
            ),
            label=f"task:{task_1_id}",
        )
        token_1_headers = {"Authorization": f"Bearer {grant_task_1.token}"}

        # 1. Upload attachment to Task 1 with scoped token -> MUST SUCCEED (201 Created)
        file_content_1 = b"Attachment content for Task 1"
        upload_1 = client.post(
            f"/tasks/{task_1_id}/attachments",
            files={"file": ("screenshot1.png", file_content_1, "image/png")},
            headers=token_1_headers,
        )
        assert upload_1.status_code == 201, f"Expected 201 Created, got {upload_1.status_code}: {upload_1.text}"
        data_1 = upload_1.json()
        assert data_1["name"] == "screenshot1.png"
        assert data_1["size"] == len(file_content_1)
        assert Path(data_1["path"]).is_file()
        assert Path(data_1["path"]).read_bytes() == file_content_1

        # Verify Task 1 metadata has attachment recorded
        get_task_1 = client.get(f"/tasks/{task_1_id}", headers=admin_headers).json()
        assert len(get_task_1["attachments"]) == 1
        assert get_task_1["attachments"][0]["name"] == "screenshot1.png"

        # 2. Upload attachment to Task 2 with same token_1 -> MUST BE REJECTED (403 Forbidden)
        file_content_2 = b"Malicious attempt to attach to Task 2"
        upload_2 = client.post(
            f"/tasks/{task_2_id}/attachments",
            files={"file": ("exploit.png", file_content_2, "image/png")},
            headers=token_1_headers,
        )
        assert upload_2.status_code == 403, f"Expected 403 Forbidden, got {upload_2.status_code}: {upload_2.text}"

        # Verify Task 2 was NOT modified
        get_task_2 = client.get(f"/tasks/{task_2_id}", headers=admin_headers).json()
        assert len(get_task_2["attachments"]) == 0
        task_2_artifact_dir = artifact_root / str(task_2_id) / "artifacts"
        if task_2_artifact_dir.exists():
            assert not (task_2_artifact_dir / "exploit.png").exists()


def test_scoped_token_isolation_with_explicit_ids_and_prefix_boundary(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Empirical test 1b (Boundary & Prefix Collision Stress-Test):
    Create tasks with explicit IDs '1', '2', and '10'.
    Verify that token scoped to 'task:1':
    - Can upload to 'task:1' (201)
    - Is rejected on 'task:2' (403)
    - Is rejected on 'task:10' (403) -> proves no prefix matching bug (e.g. task:1 matching task:10)
    """
    artifact_root = tmp_path / "artifacts"
    permissions = ExecutionPermissions()
    app = create_app(
        f"sqlite:///{migrated_db}",
        api_tokens={"administrative-token"},
        permissions=permissions,
        artifact_root=artifact_root,
    )

    db = Database.from_path(migrated_db)
    repo = TaskRepository(db)
    repo.create(title="Task One", id="1")
    repo.create(title="Task Two", id="2")
    repo.create(title="Task Ten", id="10")

    grant = permissions.issue(
        agent_id="agent-1",
        actions={"tasks.write", "tasks.read"},
        resources={"task:1", "agent:agent-1"},
        label="task:1",
    )
    headers = {"Authorization": f"Bearer {grant.token}"}

    with TestClient(app) as client:
        # 1. Upload to /tasks/1/attachments -> 201 Created
        r1 = client.post(
            "/tasks/1/attachments",
            files={"file": ("doc.txt", b"Task 1 document", "text/plain")},
            headers=headers,
        )
        assert r1.status_code == 201

        # 2. Upload to /tasks/2/attachments -> 403 Forbidden
        r2 = client.post(
            "/tasks/2/attachments",
            files={"file": ("doc.txt", b"Task 2 document", "text/plain")},
            headers=headers,
        )
        assert r2.status_code == 403

        # 3. Upload to /tasks/10/attachments -> 403 Forbidden
        r10 = client.post(
            "/tasks/10/attachments",
            files={"file": ("doc.txt", b"Task 10 document", "text/plain")},
            headers=headers,
        )
        assert r10.status_code == 403


def test_reciprocal_token_and_missing_action_isolation(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Empirical test 1c:
    - Token scoped to Task 2 can upload to Task 2, but not Task 1.
    - Read-only token scoped to Task 1 cannot upload to Task 1 (lacks tasks.write).
    - Unscoped/agent-only token cannot upload to Task 1.
    - Scoped token cannot invoke collection route POST /tasks.
    """
    artifact_root = tmp_path / "artifacts"
    permissions = ExecutionPermissions()
    app = create_app(
        f"sqlite:///{migrated_db}",
        api_tokens={"administrative-token"},
        permissions=permissions,
        artifact_root=artifact_root,
    )

    db = Database.from_path(migrated_db)
    repo = TaskRepository(db)
    repo.create(title="Task Alpha", id="alpha")
    repo.create(title="Task Beta", id="beta")

    # Token for beta
    grant_beta = permissions.issue(
        agent_id="dev",
        actions={"tasks.write"},
        resources={"task:beta"},
    )
    # Read-only token for alpha
    grant_alpha_readonly = permissions.issue(
        agent_id="reader",
        actions={"tasks.read"},
        resources={"task:alpha"},
    )
    # Token with agent resource only
    grant_agent_only = permissions.issue(
        agent_id="dev",
        actions={"tasks.write"},
        resources={"agent:dev"},
    )

    with TestClient(app) as client:
        # Beta token: upload to beta succeeds, upload to alpha rejected
        r_beta_own = client.post(
            "/tasks/beta/attachments",
            files={"file": ("b.txt", b"beta", "text/plain")},
            headers={"Authorization": f"Bearer {grant_beta.token}"},
        )
        assert r_beta_own.status_code == 201

        r_beta_foreign = client.post(
            "/tasks/alpha/attachments",
            files={"file": ("b.txt", b"beta", "text/plain")},
            headers={"Authorization": f"Bearer {grant_beta.token}"},
        )
        assert r_beta_foreign.status_code == 403

        # Read-only token rejected on alpha
        r_ro = client.post(
            "/tasks/alpha/attachments",
            files={"file": ("ro.txt", b"ro", "text/plain")},
            headers={"Authorization": f"Bearer {grant_alpha_readonly.token}"},
        )
        assert r_ro.status_code == 403

        # Agent-only token rejected on alpha
        r_agent = client.post(
            "/tasks/alpha/attachments",
            files={"file": ("agent.txt", b"agent", "text/plain")},
            headers={"Authorization": f"Bearer {grant_agent_only.token}"},
        )
        assert r_agent.status_code == 403

        # Scoped beta token rejected on collection route POST /tasks
        r_create = client.post(
            "/tasks",
            json={"title": "new task"},
            headers={"Authorization": f"Bearer {grant_beta.token}"},
        )
        assert r_create.status_code == 403


# ============================================================================
# Section 2: Empirical Tests for Worker Inbox Staging
# ============================================================================


def test_worker_inbox_staging_permissions_0o444(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """Empirical test 2a:
    Simulate TaskExecutionWorker.step() staging attachments into inbox_dir:
    - Stage file with chmod 0o444.
    - Verify the staged file in inbox_dir has exact permissions 0o444.
    """
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    artifact_root = tmp_path / "artifacts"

    # Create source file
    source_file = tmp_path / "spec_document.pdf"
    source_content = b"%PDF-1.4 sample specification content"
    source_file.write_bytes(source_content)

    # Create in_progress task with attachment
    task = task_repo.create(
        title="Execution with 0o444 staging",
        status="in_progress",
        metadata={
            "attachments": [
                {
                    "name": "spec_document.pdf",
                    "path": str(source_file),
                    "size": len(source_content),
                }
            ]
        },
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=artifact_root,
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    mock_uuid = MagicMock()
    mock_uuid.hex = "testexecution0001"

    with patch("uuid.uuid4", return_value=mock_uuid):
        outcome = worker.step()
        assert outcome is not None
        assert outcome.status == "completed"

        exec_dirs = list(artifact_root.glob(f"task_{task.id}_*"))
        assert len(exec_dirs) == 1
        inbox_dir = exec_dirs[0] / "inbox"
        assert inbox_dir.is_dir()

        staged_file = inbox_dir / "spec_document.pdf"
        assert staged_file.is_file()
        assert staged_file.read_bytes() == source_content

        # Verify exact permission 0o444
        file_stat = staged_file.stat()
        file_mode = stat.S_IMODE(file_stat.st_mode)
        assert file_mode == 0o444, f"Expected 0o444 (read-only), got {oct(file_mode)}"


def test_worker_inbox_staging_retry_overwrites_0o444_without_permission_error(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """Empirical test 2b:
    Re-run staging (simulating worker retry or re-execution):
    - Verify that overwriting or unlinking the pre-existing 0o444 file succeeds
      without raising PermissionError.
    - Verify the staged file contains the updated content and preserves 0o444.
    """
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    artifact_root = tmp_path / "artifacts"

    source_file = tmp_path / "data_feed.json"
    source_file.write_text('{"version": 1}', encoding="utf-8")

    task = task_repo.create(
        title="Retry overwrite task",
        status="in_progress",
        metadata={
            "attachments": [
                {
                    "name": "data_feed.json",
                    "path": str(source_file),
                }
            ]
        },
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=artifact_root,
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    mock_uuid = MagicMock()
    mock_uuid.hex = "retryexecution002"

    with patch("uuid.uuid4", return_value=mock_uuid):
        # 1. First execution
        outcome1 = worker.step()
        assert outcome1 is not None
        assert outcome1.status == "completed"

        exec_dirs = list(artifact_root.glob(f"task_{task.id}_*"))
        assert len(exec_dirs) == 1
        inbox_file = exec_dirs[0] / "inbox" / "data_feed.json"
        assert inbox_file.is_file()
        assert inbox_file.read_text(encoding="utf-8") == '{"version": 1}'
        assert stat.S_IMODE(inbox_file.stat().st_mode) == 0o444

        # 2. Update source file content
        source_file.write_text('{"version": 2, "updated": true}', encoding="utf-8")

        # Reset task status to in_progress to simulate retry
        t = task_repo.get(task.id)
        assert t is not None
        meta = dict(t.meta or {})
        meta.pop("execution_status", None)
        task_repo.update(task.id, status="in_progress", metadata=meta)

        # 3. Second execution (retry) over existing 0o444 file
        # Must NOT raise PermissionError
        outcome2 = worker.step()
        assert outcome2 is not None
        assert outcome2.status == "completed"

        # Verify file content is updated and permission is still 0o444
        assert inbox_file.read_text(encoding="utf-8") == '{"version": 2, "updated": true}'
        assert stat.S_IMODE(inbox_file.stat().st_mode) == 0o444


def test_worker_inbox_staging_missing_source_file_logs_warning_and_does_not_crash(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empirical test 2c:
    Test staging when an attachment source file does not exist:
    - Verify staging logs a warning.
    - Verify worker completes without crashing.
    """
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    artifact_root = tmp_path / "artifacts"

    non_existent_path = tmp_path / "ghost_file.bin"
    assert not non_existent_path.exists()

    task = task_repo.create(
        title="Task with missing attachment",
        status="in_progress",
        metadata={
            "attachments": [
                {
                    "name": "ghost_file.bin",
                    "path": str(non_existent_path),
                },
                {
                    "name": "missing_from_prior_task.png",
                    "task_id": "non-existent-prior-task",
                },
            ]
        },
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=artifact_root,
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    mock_uuid = MagicMock()
    mock_uuid.hex = "missingfile003"

    with caplog.at_level(logging.WARNING):
        with patch("uuid.uuid4", return_value=mock_uuid):
            outcome = worker.step()
            assert outcome is not None
            # Worker must complete successfully without crashing
            assert outcome.status == "completed"

    # Verify warning logged for the missing files
    warning_records = [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING and "Attachment source file not found for task" in rec.message
    ]
    assert len(warning_records) >= 1, f"Expected warning log for missing attachment, got: {caplog.text}"


def test_worker_inbox_staging_unlink_oserror_fallback_to_chmod(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
) -> None:
    """Empirical test 2d (Adversarial stress-test):
    Simulate an environment where dest_file.unlink() raises OSError
    (e.g., sticky bit / directory locking).
    Verify that staging falls back to dest_file.chmod(0o644) and successfully
    overwrites the file without crashing, resetting mode to 0o444.
    """
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    artifact_root = tmp_path / "artifacts"

    source_file = tmp_path / "config.yaml"
    source_file.write_text("setting: initial\n", encoding="utf-8")

    task = task_repo.create(
        title="Unlink failure fallback task",
        status="in_progress",
        metadata={
            "attachments": [
                {"name": "config.yaml", "path": str(source_file)}
            ]
        },
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=artifact_root,
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    mock_uuid = MagicMock()
    mock_uuid.hex = "unlinkfallback004"

    with patch("uuid.uuid4", return_value=mock_uuid):
        # First execution creates config.yaml with 0o444
        worker.step()

        exec_dirs = list(artifact_root.glob(f"task_{task.id}_*"))
        assert len(exec_dirs) == 1
        dest_file = exec_dirs[0] / "inbox" / "config.yaml"
        assert dest_file.is_file()
        assert stat.S_IMODE(dest_file.stat().st_mode) == 0o444

        # Update source content
        source_file.write_text("setting: updated\n", encoding="utf-8")

        # Reset task to in_progress
        t = task_repo.get(task.id)
        assert t is not None
        meta = dict(t.meta or {})
        meta.pop("execution_status", None)
        task_repo.update(task.id, status="in_progress", metadata=meta)

        # Mock os.unlink to raise OSError
        original_unlink = os.unlink

        def failing_unlink(path: Any, *args: Any, **kwargs: Any) -> Any:
            if "config.yaml" in str(path):
                raise OSError("Simulated unlink failure: device or resource busy")
            return original_unlink(path, *args, **kwargs)

        with patch("os.unlink", side_effect=failing_unlink):
            outcome = worker.step()
            assert outcome is not None
            assert outcome.status == "completed"

        # Verify config.yaml was overwritten despite unlink failing
        assert dest_file.read_text(encoding="utf-8") == "setting: updated\n"
        assert stat.S_IMODE(dest_file.stat().st_mode) == 0o444


def test_worker_inbox_staging_mixed_valid_invalid_traversal(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empirical test 2e:
    Comprehensive mix of attachments in one task:
    1. Valid direct path file
    2. Valid prior task artifact
    3. Path traversal attack in name ('../../escape.sh')
    4. Non-existent file
    Verify:
    - Worker does not crash
    - Valid files staged with 0o444
    - Path traversal file staged safely inside inbox as 'escape.sh' (not escaping inbox)
    - Missing file logs warning
    """
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    artifact_root = tmp_path / "artifacts"

    # 1. Direct path file
    f1 = tmp_path / "direct.txt"
    f1.write_text("direct content", encoding="utf-8")

    # 2. Prior task artifact
    prior_task_id = "prior-12345"
    prior_dir = artifact_root / prior_task_id / "artifacts"
    prior_dir.mkdir(parents=True)
    (prior_dir / "chart.png").write_bytes(b"chart bytes")

    # 3. Path traversal source
    f_trap = tmp_path / "malicious.sh"
    f_trap.write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")

    task = task_repo.create(
        title="Mixed attachments task",
        status="in_progress",
        metadata={
            "attachments": [
                {"name": "direct.txt", "path": str(f1)},
                {"name": "chart.png", "task_id": prior_task_id},
                {"name": "../../../escape.sh", "path": str(f_trap)},
                {"name": "absent.dat", "path": str(tmp_path / "does_not_exist.dat")},
            ]
        },
    )

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=artifact_root,
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    mock_uuid = MagicMock()
    mock_uuid.hex = "mixedbatch005"

    with caplog.at_level(logging.WARNING):
        with patch("uuid.uuid4", return_value=mock_uuid):
            outcome = worker.step()
            assert outcome is not None
            assert outcome.status == "completed"

    exec_dirs = list(artifact_root.glob(f"task_{task.id}_*"))
    assert len(exec_dirs) == 1
    exec_dir = exec_dirs[0]
    inbox_dir = exec_dir / "inbox"

    # direct.txt
    assert (inbox_dir / "direct.txt").read_text(encoding="utf-8") == "direct content"
    assert stat.S_IMODE((inbox_dir / "direct.txt").stat().st_mode) == 0o444

    # chart.png
    assert (inbox_dir / "chart.png").read_bytes() == b"chart bytes"
    assert stat.S_IMODE((inbox_dir / "chart.png").stat().st_mode) == 0o444

    # escape.sh safely inside inbox_dir, not in exec_dir or tmp_path
    assert (inbox_dir / "escape.sh").is_file()
    assert not (exec_dir / "escape.sh").exists()
    assert stat.S_IMODE((inbox_dir / "escape.sh").stat().st_mode) == 0o444

    # absent.dat warned
    assert any("absent.dat" in rec.message for rec in caplog.records if rec.levelno >= logging.WARNING)
