"""Tests for task attachments: schemas, repository, upload API, permissions, and worker staging."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from agent_manager.api import create_app
from agent_manager.permissions import (
    ADMIN_RESOURCE,
    ExecutionPermissions,
    actions_for_tools,
    resources_for_execution,
)
from agent_manager.storage import (
    AgentProfileRepository,
    Database,
    TaskRepository,
)
from agent_manager.task_worker import TaskExecutionWorker


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


def test_create_task_with_attachments(migrated_db: Path) -> None:
    app = create_app(f"sqlite:///{migrated_db}")
    with TestClient(app) as client:
        res = client.post(
            "/tasks",
            json={
                "title": "Task with attachments",
                "attachments": [
                    {
                        "name": "spec.pdf",
                        "path": "/tmp/spec.pdf",
                        "size": 1024,
                        "content_type": "application/pdf",
                    },
                    {
                        "name": "prior_output.json",
                        "task_id": "prev-1234",
                        "size": 512,
                    },
                ],
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert len(data["attachments"]) == 2
        assert data["attachments"][0]["name"] == "spec.pdf"
        assert data["attachments"][0]["path"] == "/tmp/spec.pdf"
        assert data["attachments"][0]["size"] == 1024
        assert data["attachments"][0]["content_type"] == "application/pdf"
        assert data["attachments"][1]["name"] == "prior_output.json"
        assert data["attachments"][1]["task_id"] == "prev-1234"

        # Verify persistence via GET
        get_res = client.get(f"/tasks/{data['id']}")
        assert get_res.status_code == 200
        get_data = get_res.json()
        assert len(get_data["attachments"]) == 2
        assert get_data["attachments"][0]["name"] == "spec.pdf"


def test_patch_task_attachments(migrated_db: Path) -> None:
    app = create_app(f"sqlite:///{migrated_db}")
    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "Patch target"}).json()
        task_id = task["id"]
        assert task["attachments"] == []

        # 1. Add attachments via PATCH
        patch_res = client.patch(
            f"/tasks/{task_id}",
            json={
                "attachments": [
                    {"name": "file1.txt", "path": "/tmp/file1.txt", "size": 10}
                ]
            },
        )
        assert patch_res.status_code == 200
        patch_data = patch_res.json()
        assert len(patch_data["attachments"]) == 1
        assert patch_data["attachments"][0]["name"] == "file1.txt"

        # 2. Clear attachments via PATCH with None
        clear_res = client.patch(
            f"/tasks/{task_id}",
            json={"attachments": None},
        )
        assert clear_res.status_code == 200
        assert clear_res.json()["attachments"] == []


def test_upload_task_attachment_and_fetch_artifact(
    migrated_db: Path, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "Upload target"}).json()
        task_id = task["id"]

        file_bytes = b"Hello, world of task attachments!"
        res = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("screenshot.png", file_bytes, "image/png")},
        )
        assert res.status_code == 201
        artifact = res.json()
        assert artifact["name"] == "screenshot.png"
        assert artifact["size"] == len(file_bytes)
        assert artifact["content_type"] == "image/png"
        assert Path(artifact["path"]).is_file()
        assert Path(artifact["path"]).read_bytes() == file_bytes

        # Check task card reflects the attachment
        updated_task = client.get(f"/tasks/{task_id}").json()
        assert len(updated_task["attachments"]) == 1
        att_record = updated_task["attachments"][0]
        assert att_record["name"] == "screenshot.png"
        assert att_record["size"] == len(file_bytes)
        assert att_record["task_id"] == task_id
        assert att_record["path"] == artifact["path"]

        # Check list artifacts endpoint includes it
        artifacts_list = client.get(f"/tasks/{task_id}/artifacts").json()
        assert any(a["name"] == "screenshot.png" for a in artifacts_list["artifacts"])


def test_upload_attachment_by_human_readable_number(
    migrated_db: Path, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "Numbered task"}).json()
        task_id = task["id"]
        number = task["number"]

        # Upload using human readable task number "1"
        res = client.post(
            f"/tasks/{number}/attachments",
            files={"file": ("notes.txt", b"meeting notes", "text/plain")},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "notes.txt"
        assert str(task_id) in data["path"]

        # Also test percent-encoded "#1"
        res_encoded = client.post(
            f"/tasks/%23{number}/attachments",
            files={"file": ("notes_encoded.txt", b"more notes", "text/plain")},
        )
        assert res_encoded.status_code == 201
        assert res_encoded.json()["name"] == "notes_encoded.txt"


def test_upload_duplicate_attachment_overwrites(
    migrated_db: Path, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "Overwrite target"}).json()
        task_id = task["id"]

        # First upload: 10 bytes
        first_bytes = b"0123456789"
        res1 = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("log.txt", first_bytes, "text/plain")},
        )
        assert res1.status_code == 201
        assert res1.json()["size"] == 10
        dest_path = Path(res1.json()["path"])
        assert dest_path.read_bytes() == first_bytes

        # Second upload: same filename, 20 bytes
        second_bytes = b"01234567890123456789"
        res2 = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("log.txt", second_bytes, "text/plain")},
        )
        assert res2.status_code == 201
        assert res2.json()["size"] == 20
        assert dest_path.read_bytes() == second_bytes

        # Verify only 1 attachment record exists in task metadata
        updated_task = client.get(f"/tasks/{task_id}").json()
        assert len(updated_task["attachments"]) == 1
        assert updated_task["attachments"][0]["name"] == "log.txt"
        assert updated_task["attachments"][0]["size"] == 20


def test_upload_attachment_path_traversal_sanitization(
    migrated_db: Path, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "Traversal target"}).json()
        task_id = task["id"]

        # Traversal attempt
        res = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("../../../../etc/passwd", b"root:x:0:0", "text/plain")},
        )
        assert res.status_code == 201
        artifact = res.json()
        assert artifact["name"] == "passwd"
        assert (artifact_root / str(task_id) / "artifacts" / "passwd").is_file()

        # Fallback for empty/dot name
        res_empty = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("..", b"empty-named", "text/plain")},
        )
        assert res_empty.status_code == 201
        assert res_empty.json()["name"] == "attachment"
        assert (artifact_root / str(task_id) / "artifacts" / "attachment").is_file()


def test_upload_attachment_missing_task_returns_404(
    migrated_db: Path, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        res = client.post(
            "/tasks/nonexistent-task-id/attachments",
            files={"file": ("test.txt", b"abc", "text/plain")},
        )
        assert res.status_code == 404
        assert res.json() == {"detail": "task not found"}


def test_upload_attachment_empty_file(
    migrated_db: Path, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "Empty file target"}).json()
        task_id = task["id"]

        res = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert res.status_code == 201
        assert res.json()["size"] == 0
        assert Path(res.json()["path"]).read_bytes() == b""


def test_upload_attachment_token_authorization(
    migrated_db: Path, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    permissions = ExecutionPermissions()
    app = create_app(
        f"sqlite:///{migrated_db}",
        artifact_root=artifact_root,
        permissions=permissions,
        api_tokens=["admin-secret-token"],
    )

    with TestClient(app) as client:
        admin_headers = {"Authorization": "Bearer admin-secret-token"}
        task1 = client.post("/tasks", json={"title": "Task 1"}, headers=admin_headers).json()["id"]
        task2 = client.post("/tasks", json={"title": "Task 2"}, headers=admin_headers).json()["id"]

        # Anonymous upload -> 401
        res_anon = client.post(
            f"/tasks/{task1}/attachments",
            files={"file": ("test.txt", b"data", "text/plain")},
        )
        assert res_anon.status_code == 401

        # Issue scoped token for task1 with tasks.write
        actions = actions_for_tools(["update_task"])
        grant1 = permissions.issue(
            agent_id="developer",
            actions=actions,
            resources=resources_for_execution(actions, task_id=task1, agent_id="developer"),
            label=f"task:{task1}",
        )
        token1_headers = {"Authorization": f"Bearer {grant1.token}"}

        # Scoped token uploads to task1 -> 201
        res_ok = client.post(
            f"/tasks/{task1}/attachments",
            files={"file": ("scoped.txt", b"task 1 file", "text/plain")},
            headers=token1_headers,
        )
        assert res_ok.status_code == 201

        # Scoped token attempts upload to task2 -> 403 Forbidden
        res_cross = client.post(
            f"/tasks/{task2}/attachments",
            files={"file": ("cross.txt", b"task 2 file", "text/plain")},
            headers=token1_headers,
        )
        assert res_cross.status_code == 403

        # Scoped token lacking tasks.write -> 403
        read_actions = actions_for_tools(["get_task"])
        read_grant = permissions.issue(
            agent_id="developer",
            actions=read_actions,
            resources=resources_for_execution(read_actions, task_id=task1),
            label=f"read:{task1}",
        )
        res_no_write = client.post(
            f"/tasks/{task1}/attachments",
            files={"file": ("test.txt", b"data", "text/plain")},
            headers={"Authorization": f"Bearer {read_grant.token}"},
        )
        assert res_no_write.status_code == 403


def test_task_worker_stages_attachments_and_handles_retries(
    tmp_path: Path,
    fake_runner_argv: list[str],
    setup_developer_agent: tuple[Database, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    test_db, agent_id = setup_developer_agent
    task_repo = TaskRepository(test_db)
    artifact_root = tmp_path / "artifacts"

    # 1. Create external file for explicit path
    ext_file = tmp_path / "external.txt"
    ext_file.write_text("external data 123", encoding="utf-8")

    # 2. Create prior task artifact
    prior_task_id = "prior-task-uuid-001"
    prior_dir = artifact_root / prior_task_id / "artifacts"
    prior_dir.mkdir(parents=True)
    (prior_dir / "diagram.png").write_bytes(b"PNG-diagram-data")

    # 3. Create target task
    task = task_repo.create(
        title="Execution task with multiple attachments",
        status="in_progress",
        metadata={
            "attachments": [
                {"name": "external.txt", "path": str(ext_file)},
                {"name": "diagram.png", "task_id": prior_task_id},
                {"name": "uploaded.json"},  # fallback to task.id
                {"name": "../../malicious.sh", "path": str(ext_file)},
                {"name": "missing.bin", "path": "/no/such/file.bin"},
            ]
        },
    )

    # Place file in target task's artifact dir for fallback check
    target_artifacts_dir = artifact_root / str(task.id) / "artifacts"
    target_artifacts_dir.mkdir(parents=True)
    (target_artifacts_dir / "uploaded.json").write_text('{"fallback": true}', encoding="utf-8")

    worker = TaskExecutionWorker(
        database=test_db,
        artifact_root=artifact_root,
        agent_id=agent_id,
        runner_argv_prefix=fake_runner_argv,
        default_workspace=str(tmp_path),
    )

    from unittest.mock import MagicMock, patch

    mock_u = MagicMock()
    mock_u.hex = "abcdef1234567890"

    with patch("uuid.uuid4", return_value=mock_u):
        outcome = worker.step()
        assert outcome is not None
        assert outcome.status == "completed"

        exec_dirs = list(artifact_root.glob(f"task_{task.id}_*"))
        assert len(exec_dirs) == 1
        inbox_dir = exec_dirs[0] / "inbox"

        # Verify external.txt
        f_ext = inbox_dir / "external.txt"
        assert f_ext.is_file()
        assert f_ext.read_text(encoding="utf-8") == "external data 123"
        assert stat.S_IMODE(f_ext.stat().st_mode) == 0o444

        # Verify diagram.png
        f_diag = inbox_dir / "diagram.png"
        assert f_diag.is_file()
        assert f_diag.read_bytes() == b"PNG-diagram-data"
        assert stat.S_IMODE(f_diag.stat().st_mode) == 0o444

        # Verify uploaded.json
        f_upl = inbox_dir / "uploaded.json"
        assert f_upl.is_file()
        assert f_upl.read_text(encoding="utf-8") == '{"fallback": true}'
        assert stat.S_IMODE(f_upl.stat().st_mode) == 0o444

        # Verify path traversal sanitized: malicious.sh is strictly in inbox
        assert (inbox_dir / "malicious.sh").is_file()
        assert not (exec_dirs[0] / "malicious.sh").exists()

        # Verify missing file warning logged
        assert "Attachment source file not found for task" in caplog.text

        # 4. Retry / Resumption defense: reset status and re-run worker step
        # Should cleanly replace 0o444 files without PermissionError
        ext_file.write_text("updated external data", encoding="utf-8")
        t = task_repo.get(task.id)
        assert t is not None
        meta = dict(t.meta or {})
        meta.pop("execution_status", None)
        task_repo.update(task.id, status="in_progress", metadata=meta)

        outcome2 = worker.step()
        assert outcome2 is not None
        assert outcome2.status == "completed"
        assert f_ext.read_text(encoding="utf-8") == "updated external data"
        assert stat.S_IMODE(f_ext.stat().st_mode) == 0o444


def test_policy_tools_declare_attachments_schema(tmp_path: Path) -> None:
    from tests.test_capabilities import _prepare_policy, _run_node
    policy = _prepare_policy(tmp_path)
    if policy is None:
        pytest.skip("Node or TypeBox not available")

    script = """
    import assert from 'node:assert/strict';
    import { pathToFileURL } from 'node:url';
    const [policyPath] = process.argv.slice(2);
    const tools = new Map();
    const { default: registerPolicy } = await import(pathToFileURL(policyPath));
    registerPolicy({ on() {}, registerTool(tool) { tools.set(tool.name, tool); } });

    for (const name of ['create_task', 'update_task']) {
      const tool = tools.get(name);
      assert.ok(tool, `Tool ${name} not registered`);
      assert.ok(tool.parameters.properties.attachments, `attachments parameter missing in ${name}`);
      assert.equal(tool.parameters.properties.attachments.type, 'array');
      assert.equal(tool.parameters.properties.attachments.items.type, 'object');
      const itemProps = tool.parameters.properties.attachments.items.properties;
      assert.ok(itemProps.name, 'item name property missing');
      assert.ok(itemProps.path, 'item path property missing');
      assert.ok(itemProps.task_id, 'item task_id property missing');
    }
    console.log("SCHEMA_OK");
    """
    script_file = tmp_path / "check_schema.mjs"
    script_file.write_text(script, encoding="utf-8")
    result = _run_node(script_file, str(policy), cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCHEMA_OK" in result.stdout

