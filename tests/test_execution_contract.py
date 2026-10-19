"""Secretary-to-worker execution contracts, using temporary durable storage."""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_manager.api import create_app
from agent_manager.runtime_defaults import RuntimeDefaultsResolver
from agent_manager.storage import AgentProfileRepository, Database, TaskRepository
from agent_manager.task_worker import TaskExecutionWorker


def test_task_assignment_roundtrip(migrated_db: Path, tmp_path: Path):
    with TestClient(create_app(f"sqlite:///{migrated_db}")) as client:
        agent = client.post(
            "/agents",
            json={
                "agent_id": "scout",
                "name": "Scout",
                "role": "analyzer",
                "system_prompt": "Read only.",
            },
        )
        assert agent.status_code == 201
        created = client.post(
            "/tasks",
            json={
                "title": "Find project",
                "agent_id": "scout",
                "workspace": str(tmp_path),
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["agent_id"] == "scout"
        task_id = created.json()["id"]
        changed = client.patch(
            f"/tasks/{task_id}",
            json={"agent_id": None, "workspace": str(tmp_path / "other")},
        )
        assert changed.status_code == 200
        assert changed.json()["agent_id"] is None
        assert changed.json()["workspace"] == str(tmp_path / "other")
        assert client.get(f"/tasks/{task_id}").json()["agent_id"] is None


def test_workspace_preflight_reports_real_mount_and_rejects_missing(
    migrated_db: Path, tmp_path: Path
):
    with TestClient(
        create_app(f"sqlite:///{migrated_db}", default_workspace=tmp_path)
    ) as client:
        client.post(
            "/agents",
            json={
                "agent_id": "agent-manager-developer",
                "name": "Default",
                "role": "developer",
                "system_prompt": "Default.",
            },
        )
        response = client.get(
            "/workspaces/resolve", params={"workspace": str(tmp_path)}
        )
        assert response.status_code == 200, response.text
        assert response.json()["host_path"] == str(tmp_path.resolve())
        assert response.json()["sandbox_path"] == "/workspace"
        missing = client.get(
            "/workspaces/resolve", params={"workspace": str(tmp_path / "missing")}
        )
        assert missing.status_code == 422
        assert "missing" in missing.json()["detail"]


def test_worker_uses_assigned_agent_and_runtime_policy(
    migrated_db: Path, tmp_path: Path
):
    db = Database.from_path(migrated_db)
    try:
        AgentProfileRepository(db).create(
            agent_id="scout",
            name="Scout",
            role="analyzer",
            system_prompt="Scout only.",
            runner_config={
                "mode": "success",
                "summary_text": "Found project.",
                "project_read_only": True,
                "network": False,
            },
        )
        task = TaskRepository(db).create(
            title="Find project",
            status="in_progress",
            workspace=str(tmp_path),
            agent_id="scout",
            metadata={"task_type": "research"},
        )
        defaults = tmp_path / "runtime.toml"
        defaults.write_text('[default]\nprovider="test-provider"\nmodel="test-model"\n')
        worker = TaskExecutionWorker(
            db,
            tmp_path / "artifacts",
            runtime_defaults=RuntimeDefaultsResolver(
                app_config_path=defaults,
                global_settings_path=tmp_path / "missing-settings.json",
            ),
            runner_argv_prefix=[
                sys.executable,
                str(Path(__file__).with_name("fake_pi_runner.py")),
            ],
        )
        result = worker.step()
        assert result.status == "completed", result.error
        role = next((tmp_path / "artifacts").rglob("role.json"))
        config = json.loads(role.read_text())
        assert config["agent_id"] == "scout"
        assert config["provider"] == "test-provider"
        assert config["model"] == "test-model"
        assert config["project"] == str(tmp_path)
        assert config["project_read_only"] is True
        assert config["network"] is False
        assert "pi_command" in config and "runtime_paths" in config
        stored = TaskRepository(db).get(task.id)
        assert stored.meta["execution_context"]["agent_id"] == "scout"
        assert stored.meta["execution_context"]["sandbox_path"] == "/workspace"
    finally:
        db.dispose()


def test_unknown_workspace_fails_without_launch_or_fallback(
    migrated_db: Path, tmp_path: Path
):
    db = Database.from_path(migrated_db)
    try:
        AgentProfileRepository(db).create(
            agent_id="agent-manager-developer",
            name="Default",
            role="developer",
            system_prompt="Default.",
        )
        TaskRepository(db).create(
            title="Find project",
            status="in_progress",
            workspace=str(tmp_path / "missing"),
        )
        worker = TaskExecutionWorker(
            db,
            tmp_path / "artifacts",
            default_workspace=tmp_path,
            runner_argv_prefix=["must-not-launch"],
        )
        result = worker.step()
        assert result.status == "failed"
        assert "workspace" in result.error.lower() and "missing" in result.error
        assert not list((tmp_path / "artifacts").rglob("role.json"))
    finally:
        db.dispose()


def test_api_requires_assignment_migration(db_path: Path):
    from alembic import command

    from .conftest import alembic_config

    command.upgrade(alembic_config(db_path), "0005_create_conversation_runtimes")
    with (
        pytest.raises(RuntimeError, match="tasks.agent_id"),
        TestClient(create_app(f"sqlite:///{db_path}")),
    ):
        pass


def test_missing_assigned_agent_never_runs_default(migrated_db: Path, tmp_path: Path):
    db = Database.from_path(migrated_db)
    try:
        AgentProfileRepository(db).create(
            agent_id="agent-manager-developer",
            name="Default",
            role="developer",
            system_prompt="Wrong agent.",
        )
        TaskRepository(db).create(
            title="Find project",
            agent_id="missing-scout",
            workspace=str(tmp_path),
            status="in_progress",
        )
        worker = TaskExecutionWorker(
            db, tmp_path / "artifacts", runner_argv_prefix=["must-not-launch"]
        )
        result = worker.step()
        assert result.status == "failed"
        assert "missing-scout" in result.error
        assert not list((tmp_path / "artifacts").rglob("role.json"))
    finally:
        db.dispose()


def test_preflight_rejects_unknown_agent(migrated_db: Path, tmp_path: Path):
    with TestClient(create_app(f"sqlite:///{migrated_db}")) as client:
        response = client.get(
            "/workspaces/resolve",
            params={"workspace": str(tmp_path), "agent_id": "missing"},
        )
        assert response.status_code == 404


def test_profile_markdown_preserves_reviewed_policy_and_skills(
    migrated_db: Path, tmp_path: Path
):
    from agent_manager.knowledge import (
        KnowledgeBase,
        format_agent_profile_markdown,
        parse_agent_profile_markdown,
    )

    db = Database.from_path(migrated_db)
    try:
        repo = AgentProfileRepository(db)
        profile = repo.create(
            agent_id="scout",
            name="Scout",
            role="analyzer",
            system_prompt="Approved.",
            runner_config={
                "project": str(tmp_path),
                "network": True,
                "project_read_only": True,
                "tools": ["read", "bash"],
            },
            skills=[{"skill": "codespaces"}],
        )
        markdown = format_agent_profile_markdown(profile)
        parsed = parse_agent_profile_markdown(markdown)
        assert parsed["runner_config"]["project_read_only"] is True
        assert parsed["runner_config"]["project"] == str(tmp_path)
        assert parsed["skills"] == [{"skill": "codespaces"}]
        kb = KnowledgeBase(tmp_path / "knowledge")
        kb.agents_dir.mkdir(parents=True)
        (kb.agents_dir / "scout.md").write_text(markdown)
        assert kb.sync_agent_from_file("scout", db) is False
        assert repo.get_current("scout").revision == 1
    finally:
        db.dispose()
