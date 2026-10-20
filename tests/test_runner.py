"""Tests for the unified agent_manager.runner."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_manager.permissions import actions_for_tools, resources_for_execution
from agent_manager.runner import (
    AgentManagerServices,
    RunnerConfig,
    run_database_migrations,
)
from agent_manager.telegram.client import BackendClient
from agent_manager.telegram.config import TelegramConfig


def test_runner_config_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_MANAGER_DB_URL", raising=False)
    monkeypatch.delenv("AGENT_MANAGER_HOST", raising=False)
    monkeypatch.delenv("AGENT_MANAGER_PORT", raising=False)
    monkeypatch.delenv("AGENT_MANAGER_TASK_CONCURRENCY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    cfg = RunnerConfig.from_env()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 18767
    assert cfg.task_concurrency == 3
    assert cfg.auto_migrate is True
    assert cfg.telegram_config is None


def test_runner_config_custom_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_MANAGER_DB_URL", f"sqlite:///{tmp_path}/custom.db")
    monkeypatch.setenv("AGENT_MANAGER_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_MANAGER_PORT", "9000")
    monkeypatch.setenv("AGENT_MANAGER_AUTO_MIGRATE", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock-token-xyz")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "12345")

    cfg = RunnerConfig.from_env()
    assert cfg.database_url == f"sqlite:///{tmp_path}/custom.db"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert cfg.auto_migrate is False
    assert cfg.telegram_config is not None
    assert cfg.telegram_config.bot_token == "mock-token-xyz"


def test_agent_manager_services_setup_and_lifecycle(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    db_url = f"sqlite:///{migrated_db}"
    cfg = RunnerConfig(
        database_url=db_url,
        host="127.0.0.1",
        port=19999,
        artifact_root=tmp_path / "artifacts",
        knowledge_root=tmp_path / "knowledge",
        state_root=tmp_path / "state",
        workspaces_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
        default_workspace=tmp_path / "workspace",
        auto_migrate=False,
    )

    loop = asyncio.new_event_loop()
    try:
        services = AgentManagerServices(cfg)
        services.setup(loop)

        assert services.database is not None
        assert services.queue_dispatcher is not None
        assert services.task_worker is not None
        assert services.uvicorn_server is not None

        services.start_background_threads()
        assert services.queue_dispatcher._control_thread is not None
        assert services.queue_dispatcher._control_thread.is_alive()
        assert len(services.task_worker.threads) == cfg.task_concurrency
        assert all(thread.is_alive() for thread in services.task_worker.threads)

        services.stop_background_threads()
        assert not services.queue_dispatcher._control_thread.is_alive()
        assert not any(thread.is_alive() for thread in services.task_worker.threads)
    finally:
        loop.close()


class _LazyApp:
    """ASGI proxy that can be bound to the app after the service builds it."""

    def __init__(self) -> None:
        self.app = None

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        return await self.app(scope, receive, send)


def test_service_created_telegram_client_uses_host_internal_principal(
    migrated_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real service wiring, bot client and child credential are tested.

    No caller-supplied administrative token exists. The unified service must
    mint an internal host principal and hand it to the actual BackendClient
    built by ``create_bot``. That client must work before any execution
    grant, during a secretary execution and after the grant is revoked, while
    a scoped child credential cannot reach administrative routes.
    """
    telegram_config = TelegramConfig(
        bot_token="123456789:TEST_BOT_TOKEN_FOR_MOCKING_ONLY_123",
        allowed_user_id=123,
        backend_url="http://testserver",
        api_token=None,
        deepgram_api_key=None,
        state_db_path=tmp_path / "telegram_state.db",
        media_dir=tmp_path / "telegram_media",
    )
    cfg = RunnerConfig(
        database_url=f"sqlite:///{migrated_db}",
        artifact_root=tmp_path / "artifacts",
        knowledge_root=tmp_path / "knowledge",
        state_root=tmp_path / "state",
        workspaces_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
        default_workspace=tmp_path / "workspace",
        backend_url="http://testserver",
        auto_migrate=False,
        telegram_config=telegram_config,
    )
    services = AgentManagerServices(cfg)

    # Route the httpx client created inside the real BackendClient to the
    # in-process ASGI app without opening a network socket. The app does not
    # exist until setup() runs, so the transport talks to a lazy proxy.
    lazy_app = _LazyApp()
    transport = httpx.ASGITransport(app=lazy_app)
    real_async_client = httpx.AsyncClient

    def _async_client_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("base_url", None)
        kwargs["base_url"] = "http://testserver"
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "agent_manager.telegram.client.httpx.AsyncClient",
        _async_client_factory,
    )

    loop = asyncio.new_event_loop()
    try:
        services.setup(loop)
    finally:
        loop.close()

    assert services.uvicorn_server is not None
    app = services.uvicorn_server.config.app
    lazy_app.app = app

    assert services.internal_token, "service must mint an internal token"
    assert services.internal_token not in cfg.api_tokens
    assert app.state.require_auth is True
    assert services.queue_processor is not None
    telegram_client = services.queue_processor.client
    assert telegram_client.api_token == services.internal_token

    with TestClient(app) as client:
        # Anonymous mutation is denied from startup, before any grant exists.
        assert client.get("/tasks").status_code == 401

        # The internal host client works before any execution grant.
        created = asyncio.run(telegram_client.create_task(title="from telegram"))
        assert created["title"] == "from telegram"
        listed = asyncio.run(telegram_client.list_tasks())
        assert any(item["id"] == created["id"] for item in listed)

        # A secretary execution issues a scoped child credential.
        child_actions = actions_for_tools(["get_task"])
        child_grant = services.permissions.issue(
            agent_id="agent-manager-developer",
            actions=child_actions,
            resources=resources_for_execution(
                child_actions,
                task_id=created["id"],
                agent_id="agent-manager-developer",
            ),
            label=f"task:{created['id']}",
        )
        child_headers = {"Authorization": f"Bearer {child_grant.token}"}

        # The internal host client keeps working while a child grant exists.
        during = asyncio.run(telegram_client.create_task(title="during turn"))
        assert during["id"] != created["id"]

        # The child credential is read-scoped and cannot gain admin access.
        assert client.get(f"/tasks/{created['id']}", headers=child_headers).status_code == 200
        assert client.get("/tasks", headers=child_headers).status_code == 403
        assert client.post(
            "/tasks", json={"title": "child"}, headers=child_headers
        ).status_code == 403
        assert client.get("/agents", headers=child_headers).status_code == 403

        # When the execution finishes its grant is revoked; anonymous access
        # stays closed and the internal host client is unaffected.
        services.permissions.revoke(child_grant.token)
        assert client.get("/tasks").status_code == 401
        assert client.get(
            f"/tasks/{created['id']}", headers=child_headers
        ).status_code == 401
        assert asyncio.run(telegram_client.list_tasks())

    # The internal host credential never leaks into a child document.
    assert child_grant.token != services.internal_token
