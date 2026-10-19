"""Tests for the unified agent_manager.runner."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_manager.runner import (
    AgentManagerServices,
    RunnerConfig,
    run_database_migrations,
)
from agent_manager.telegram.config import TelegramConfig


def test_runner_config_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_MANAGER_DB_URL", raising=False)
    monkeypatch.delenv("AGENT_MANAGER_HOST", raising=False)
    monkeypatch.delenv("AGENT_MANAGER_PORT", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    cfg = RunnerConfig.from_env()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 18767
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
        assert services.task_worker._thread is not None
        assert services.task_worker._thread.is_alive()

        services.stop_background_threads()
        assert not services.queue_dispatcher._control_thread.is_alive()
        assert not services.task_worker._thread.is_alive()
    finally:
        loop.close()
