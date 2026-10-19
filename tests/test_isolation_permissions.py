"""Instance isolation and execution-scoped API permission tests.

These tests exercise the real middleware, the real worker role-config export
and the real knowledge bootstrap. They never use an in-memory shortcut: the
permission registry is the one connected to the FastAPI application under
test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_manager.api import create_app
from agent_manager.instance import InstanceContext
from agent_manager.knowledge import KnowledgeBase
from agent_manager.permissions import (
    ADMIN_RESOURCE,
    ExecutionPermissions,
    actions_for_tools,
    resolve_requirement,
    resources_for_execution,
)
from agent_manager.runner import RunnerConfig
from agent_manager.runtime_defaults import RuntimeDefaultsResolver
from agent_manager.storage import AgentProfileRepository, Database, TaskRepository
from agent_manager.task_worker import TaskExecutionWorker

from .conftest import upgrade_to_head


class _RecordingPermissions(ExecutionPermissions):
    """ExecutionPermissions that records every issued grant for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.issued: list[Any] = []

    def issue(self, **kwargs: Any) -> Any:
        grant = super().issue(**kwargs)
        self.issued.append(grant)
        return grant


def _admin_client(migrated_db: Path, permissions: ExecutionPermissions) -> TestClient:
    app = create_app(
        f"sqlite:///{migrated_db}",
        api_tokens={"administrative-token"},
        permissions=permissions,
    )
    return TestClient(app)


def _scoped_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_actions_are_derived_from_configured_tools_only() -> None:
    assert actions_for_tools(["read", "bash"]) == frozenset()
    assert actions_for_tools(["get_task"]) == frozenset({"tasks.read"})
    assert actions_for_tools(["create_task"]) == frozenset({"tasks.write"})
    # An unknown tool cannot widen the grant.
    assert actions_for_tools(["root_everything"]) == frozenset()


def test_resource_scope_for_read_only_execution_is_task_and_agent() -> None:
    resources = resources_for_execution(
        actions_for_tools(["get_task", "get_agent"]),
        task_id="t1",
        agent_id="reviewer",
    )
    assert resources == frozenset({"task:t1", "agent:reviewer"})


def test_management_action_requires_wildcard_resource() -> None:
    resources = resources_for_execution(
        actions_for_tools(["create_task"]),
        task_id="t1",
        agent_id="secretary",
    )
    assert resources == frozenset({ADMIN_RESOURCE})


def test_route_requirement_resolves_action_and_resource() -> None:
    requirement = resolve_requirement("GET", "/tasks/t1")
    assert requirement is not None
    assert requirement.action == "tasks.read"
    assert requirement.resource == "task:t1"
    # A collection route is not constrained to a single resource.
    collection = resolve_requirement("GET", "/tasks")
    assert collection is not None and collection.resource is None
    assert resolve_requirement("GET", "/health") is None


def test_reviewer_and_analyzer_tokens_cannot_widen_even_via_raw_http(
    migrated_db: Path,
) -> None:
    permissions = ExecutionPermissions()
    with _admin_client(migrated_db, permissions) as client:
        admin = {"Authorization": "Bearer administrative-token"}
        created = client.post(
            "/tasks",
            json={"title": "review me", "workspace": "/tmp"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

    # Reviewer/analyzer sandboxes are filesystem-only: no tool maps to an API
    # action, so their execution credential carries no authority at all.
    actions = actions_for_tools(["read", "bash"])
    grant = permissions.issue(
        agent_id="system-code-reviewer",
        actions=actions,
        resources=resources_for_execution(actions, task_id=task_id),
        label="reviewer",
    )
    with _admin_client(migrated_db, permissions) as client:
        # Raw HTTP with the sandbox credential: reads of its own task, agent
        # mutation, task creation and workspace preflight are all rejected.
        assert client.get(
            f"/tasks/{task_id}", headers=_scoped_headers(grant.token)
        ).status_code == 403
        assert client.patch(
            "/agents/system-code-reviewer",
            json={"expected_revision": 1, "system_prompt": "evil"},
            headers=_scoped_headers(grant.token),
        ).status_code == 403
        assert client.post(
            "/tasks",
            json={"title": "created by reviewer"},
            headers=_scoped_headers(grant.token),
        ).status_code == 403
        assert client.get(
            "/workspaces/resolve",
            params={"workspace": "/tmp"},
            headers=_scoped_headers(grant.token),
        ).status_code == 403
        assert client.post(
            "/tasks",
            json={"title": "evil"},
            headers={"Authorization": "Bearer administrative-token"},
        ).status_code == 201


def test_scoped_read_token_reads_only_its_own_task(migrated_db: Path) -> None:
    permissions = ExecutionPermissions()
    with _admin_client(migrated_db, permissions) as client:
        admin = {"Authorization": "Bearer administrative-token"}
        first = client.post(
            "/tasks", json={"title": "first", "workspace": "/tmp"}, headers=admin
        ).json()["id"]
        second = client.post(
            "/tasks", json={"title": "second", "workspace": "/tmp"}, headers=admin
        ).json()["id"]

    actions = actions_for_tools(["get_task"])
    assert actions == frozenset({"tasks.read"})
    grant = permissions.issue(
        agent_id="developer",
        actions=actions,
        resources=resources_for_execution(actions, task_id=first, agent_id="developer"),
    )

    with _admin_client(migrated_db, permissions) as client:
        headers = _scoped_headers(grant.token)
        allowed = client.get(f"/tasks/{first}", headers=headers)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["id"] == first
        assert client.get(f"/tasks/{second}", headers=headers).status_code == 403
        assert client.get("/tasks", headers=headers).status_code == 403
        assert client.get("/agents", headers=headers).status_code == 403


def test_wildcard_read_grant_can_list_tasks(migrated_db: Path) -> None:
    permissions = ExecutionPermissions()
    grant = permissions.issue(
        agent_id="secretary",
        actions=frozenset({"tasks.read"}),
        resources=frozenset({ADMIN_RESOURCE}),
    )
    with _admin_client(migrated_db, permissions) as client:
        response = client.get("/tasks", headers=_scoped_headers(grant.token))
        assert response.status_code == 200
        assert response.json() == []


def test_distinct_instances_have_independent_files_urls_and_profiles(
    tmp_path: Path,
) -> None:
    first_db_path = tmp_path / "one" / "one.db"
    second_db_path = tmp_path / "two" / "two.db"
    first_db_path.parent.mkdir(parents=True)
    second_db_path.parent.mkdir(parents=True)
    upgrade_to_head(first_db_path)
    upgrade_to_head(second_db_path)

    first_cfg = RunnerConfig(
        database_url=f"sqlite:///{first_db_path}",
        artifact_root=tmp_path / "one" / "artifacts",
        knowledge_root=tmp_path / "one" / "knowledge",
        state_root=tmp_path / "one" / "state",
        workspaces_root=tmp_path / "one" / "workspaces",
        skills_root=tmp_path / "one" / "skills",
        default_workspace=tmp_path / "one" / "project",
        backend_url="http://127.0.0.1:19001",
        openviking_url="http://127.0.0.1:19002",
    )
    second_cfg = RunnerConfig(
        database_url=f"sqlite:///{second_db_path}",
        artifact_root=tmp_path / "two" / "artifacts",
        knowledge_root=tmp_path / "two" / "knowledge",
        state_root=tmp_path / "two" / "state",
        workspaces_root=tmp_path / "two" / "workspaces",
        skills_root=tmp_path / "two" / "skills",
        default_workspace=tmp_path / "two" / "project",
        backend_url="http://127.0.0.1:19101",
        openviking_url="http://127.0.0.1:19102",
    )
    first = first_cfg.instance_context()
    second = second_cfg.instance_context()
    assert first.backend_url != second.backend_url
    assert first.state_root != second.state_root
    assert first.knowledge_root != second.knowledge_root

    first_db = Database.from_path(first_db_path)
    second_db = Database.from_path(second_db_path)
    try:
        KnowledgeBase(first.knowledge_root, instance=first).initialize(
            database=first_db
        )
        KnowledgeBase(second.knowledge_root, instance=second).initialize(
            database=second_db
        )
        assert (first.knowledge_root / "agents/system-secretary.md").is_file()
        assert (second.knowledge_root / "agents/system-secretary.md").is_file()
        assert not (first.knowledge_root / "agents").samefile(
            second.knowledge_root / "agents"
        )
        # Profile state is instance-local: each database has its own
        # developer profile pointing at its own explicit workspace.
        first_dev = AgentProfileRepository(first_db).get_current(
            "agent-manager-developer"
        )
        second_dev = AgentProfileRepository(second_db).get_current(
            "agent-manager-developer"
        )
        assert first_dev is not None and second_dev is not None
        assert first_dev.runner_config["project"] == str(
            tmp_path / "one" / "project"
        )
        assert second_dev.runner_config["project"] == str(
            tmp_path / "two" / "project"
        )
        assert first.state_root.is_dir() and second.state_root.is_dir()
        # Tokens issued by one instance registry are unknown to the other.
        first_grant = ExecutionPermissions().issue(
            agent_id="a", actions=[], resources=["task:t"]
        )
        second_permissions = ExecutionPermissions()
        second_permissions.issue(agent_id="b", actions=[], resources=["task:t"])
        assert second_permissions.resolve(first_grant.token) is None
    finally:
        first_db.dispose()
        second_db.dispose()


def test_worker_issues_scoped_backend_and_never_admin_token(
    migrated_db: Path, tmp_path: Path
) -> None:
    db = Database.from_path(migrated_db)
    try:
        instance = InstanceContext(
            state_root=tmp_path / "state",
            workspaces_root=tmp_path / "workspaces",
            skills_root=tmp_path / "skills",
            knowledge_root=tmp_path / "knowledge",
            artifact_root=tmp_path / "artifacts",
            backend_url="http://127.0.0.1:19999",
        )
        instance.state_root.mkdir(parents=True)
        instance.skills_root.mkdir(parents=True)
        instance.knowledge_root.mkdir(parents=True)
        AgentProfileRepository(db).create(
            agent_id="scout",
            name="Scout",
            role="analyzer",
            system_prompt="Scout only.",
            runner_config={
                "mode": "success",
                "summary_text": "Found project.",
                "tools": ["get_task"],
            },
        )
        task = TaskRepository(db).create(
            title="Find project",
            status="in_progress",
            workspace=str(tmp_path),
            agent_id="scout",
        )
        permissions = _RecordingPermissions()
        worker = TaskExecutionWorker(
            db,
            tmp_path / "artifacts",
            runner_argv_prefix=[
                sys.executable,
                str(Path(__file__).with_name("fake_pi_runner.py")),
            ],
            instance=instance,
            permissions=permissions,
            runtime_defaults=RuntimeDefaultsResolver(
                global_settings_path=tmp_path / "missing-settings.json"
            ),
        )
        result = worker.step()
        assert result is not None and result.status == "completed", result

        role = next((tmp_path / "artifacts").rglob("role.json"))
        role_text = role.read_text()
        config = json.loads(role_text)
        assert config["knowledge_dir"] == str(instance.knowledge_root)
        assert config["skills_dir"] == str(instance.skills_root)
        backend = config["backend"]
        assert backend["backend_url"] == instance.backend_url
        scoped_token = backend["api_token"]
        assert permissions.issued, "worker must issue a scoped execution grant"
        grant = permissions.issued[-1]
        assert grant.token == scoped_token
        assert grant.agent_id == "scout"
        assert grant.actions == frozenset({"tasks.read"})
        assert f"task:{task.id}" in grant.resources
        # The grant is revoked when the execution finishes; revocation never
        # reopens the protected API because auth mode is fixed at startup.
        assert permissions.resolve(scoped_token) is None
        # The administrative token value was never embedded in the child
        # role configuration or credential document.
        assert "administrative-token" not in role_text
    finally:
        db.dispose()
