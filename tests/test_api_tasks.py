"""Task API tests for status counters exposed to the Telegram bot."""

from __future__ import annotations

import asyncio
import pathlib

import httpx
from fastapi.testclient import TestClient

from agent_manager.api import create_app
from agent_manager.telegram.client import BackendClient


def _app(db_path: pathlib.Path):
    return create_app(f"sqlite:///{db_path}")


def test_task_counts_returns_status_mapping(migrated_db: pathlib.Path) -> None:
    with TestClient(_app(migrated_db)) as client:
        for title, status in (
            ("first incoming", "incoming"),
            ("second incoming", "incoming"),
            ("working", "in_progress"),
            ("failed", "failed"),
            ("archived", "archived"),
            ("custom", "a-status-not-in-any-list"),
        ):
            created = client.post(
                "/tasks", json={"title": title, "status": status}
            )
            assert created.status_code == 201

        response = client.get("/tasks/counts")

    assert response.status_code == 200
    assert response.json() == {
        "a-status-not-in-any-list": 1,
        "archived": 1,
        "failed": 1,
        "incoming": 2,
        "in_progress": 1,
    }


def test_task_counts_is_empty_when_no_tasks_exist(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        response = client.get("/tasks/counts")

    assert response.status_code == 200
    assert response.json() == {}


def test_task_counts_route_is_not_shadowed_by_task_lookup(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        counts = client.get("/tasks/counts")
        task = client.get("/tasks/does-not-exist")

    assert counts.status_code == 200
    assert counts.json() == {}
    assert task.status_code == 404


def test_backend_client_fetches_task_counts(migrated_db: pathlib.Path) -> None:
    app = _app(migrated_db)

    async def scenario() -> dict[str, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http_client:
            async with BackendClient(
                base_url="http://testserver", client=http_client
            ) as backend:
                await backend.create_task(title="one", status="incoming")
                await backend.create_task(title="two", status="incoming")
                await backend.create_task(title="three", status="done")
                return await backend.get_task_counts()

    with TestClient(app):
        assert asyncio.run(scenario()) == {"done": 1, "incoming": 2}
