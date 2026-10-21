"""Acceptance and unit tests for Bearer token authentication."""

from __future__ import annotations

from pathlib import Path
import pytest
from fastapi.testclient import TestClient
import httpx

from kuzya.api import create_app
from kuzya.permissions import ExecutionPermissions
from kuzya.telegram.client import BackendClient, BackendError


def test_api_auth_open_mode_when_no_tokens(migrated_db: Path) -> None:
    app = create_app(f"sqlite:///{migrated_db}", api_tokens=[])
    # Open mode is now explicit: neither a registry nor admin tokens exist.
    assert app.state.require_auth is False
    with TestClient(app) as client:
        # /health is 200
        res = client.get("/health")
        assert res.status_code == 200

        # /tasks is 200 without token
        res = client.get("/tasks")
        assert res.status_code == 200
        # The deliberate development choice also allows anonymous mutation.
        created = client.post("/tasks", json={"title": "dev task"})
        assert created.status_code == 201


def test_registry_auth_mode_stays_protected_across_grants(
    migrated_db: Path,
) -> None:
    """Auth mode is a stable per-instance decision, not a grant count."""
    permissions = ExecutionPermissions()
    app = create_app(f"sqlite:///{migrated_db}", permissions=permissions)
    assert app.state.require_auth is True
    with TestClient(app) as client:
        # (1) Before any grant the protected API denies anonymous mutation.
        assert client.get("/tasks").status_code == 401
        assert client.post("/tasks", json={"title": "anon"}).status_code == 401

        grant = permissions.issue(
            agent_id="developer",
            actions=["tasks.read"],
            resources=["task:t1"],
        )
        assert permissions.resolve(grant.token) is not None
        permissions.revoke(grant.token)
        assert len(permissions) == 0
        assert permissions.resolve(grant.token) is None

        # (2) Revoking the last grant must not reopen anonymous access.
        assert client.get("/tasks").status_code == 401
        assert client.post("/tasks", json={"title": "anon"}).status_code == 401


def test_admin_token_alone_protects_without_registry(migrated_db: Path) -> None:
    app = create_app(
        f"sqlite:///{migrated_db}", api_tokens={"admin-token"}
    )
    assert app.state.require_auth is True
    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 401
        admin = {"Authorization": "Bearer admin-token"}
        assert client.get("/tasks", headers=admin).status_code == 200


def test_api_auth_enforced_with_multiple_tokens(migrated_db: Path) -> None:
    allowed_tokens = {"token-secret-alpha", "token-secret-beta"}
    app = create_app(f"sqlite:///{migrated_db}", api_tokens=allowed_tokens)
    with TestClient(app) as client:
        # 1. /health is always open
        res = client.get("/health")
        assert res.status_code == 200

        # 2. Endpoints without token return 401
        res = client.get("/tasks")
        assert res.status_code == 401
        assert "WWW-Authenticate" in res.headers
        assert "Unauthorized" in res.json()["detail"]

        # 3. Invalid token returns 401
        res = client.get("/tasks", headers={"Authorization": "Bearer bad-token"})
        assert res.status_code == 401

        # 4. First valid token succeeds
        res1 = client.get("/tasks", headers={"Authorization": "Bearer token-secret-alpha"})
        assert res1.status_code == 200
        assert isinstance(res1.json(), list)

        # 5. Second valid token succeeds
        res2 = client.get("/tasks", headers={"Authorization": "Bearer token-secret-beta"})
        assert res2.status_code == 200
        assert isinstance(res2.json(), list)


def test_backend_client_with_bearer_token(migrated_db: Path) -> None:
    allowed_tokens = {"token-backend-client"}
    app = create_app(f"sqlite:///{migrated_db}", api_tokens=allowed_tokens)

    with TestClient(app):
        transport = httpx.ASGITransport(app=app)
        async_http_client = httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        )

        # 1. Client with valid token
        client = BackendClient(
            base_url="http://testserver",
            api_token="token-backend-client",
            client=async_http_client,
        )

        import asyncio

        tasks = asyncio.run(client.list_tasks())
        assert isinstance(tasks, list)

        created = asyncio.run(client.create_task(title="Auth task"))
        assert created["title"] == "Auth task"

        # 2. Client with invalid token raises BackendError(401)
        bad_client = BackendClient(
            base_url="http://testserver",
            api_token="invalid-token",
            client=async_http_client,
        )

        with pytest.raises(BackendError) as exc_info:
            asyncio.run(bad_client.list_tasks())
        assert exc_info.value.status_code == 401


def test_delete_knowledge_requires_knowledge_write_scope(
    migrated_db: Path, tmp_path: Path
) -> None:
    from kuzya.knowledge import KnowledgeBase
    from kuzya.permissions import (
        ExecutionPermissions,
        actions_for_tools,
        resources_for_execution,
    )

    knowledge = KnowledgeBase(tmp_path / "knowledge")
    knowledge.initialize()
    document = knowledge.root_dir / "docs/obsolete.md"
    document.parent.mkdir(parents=True)
    document.write_text("# Obsolete\n", encoding="utf-8")

    permissions = ExecutionPermissions()
    reader = permissions.issue(
        agent_id="system-secretary",
        actions=actions_for_tools(["read_knowledge", "list_knowledge"]),
        resources=resources_for_execution(
            actions_for_tools(["read_knowledge", "list_knowledge"])
        ),
    )
    writer_actions = actions_for_tools(["delete_knowledge"])
    assert writer_actions == frozenset({"knowledge.write"})
    writer = permissions.issue(
        agent_id="system-secretary",
        actions=writer_actions,
        resources=resources_for_execution(writer_actions),
    )

    app = create_app(
        f"sqlite:///{migrated_db}",
        knowledge_base=knowledge,
        permissions=permissions,
    )
    with TestClient(app) as client:
        assert client.delete("/knowledge/docs/obsolete.md").status_code == 401

        denied = client.delete(
            "/knowledge/docs/obsolete.md",
            headers={"Authorization": f"Bearer {reader.token}"},
        )
        assert denied.status_code == 403
        assert document.is_file()

        allowed = client.delete(
            "/knowledge/docs/obsolete.md",
            headers={"Authorization": f"Bearer {writer.token}"},
        )
        assert allowed.status_code == 200, allowed.text
        assert not document.exists()
