"""Acceptance tests for the /mcp endpoint.

Covers the test programme of ``docs/tasks/mcp-endpoint-spec.md`` §6:
initialization, tool listing against the OpenAPI surface, Bearer
authentication, reads and writes through generated tools, error and conflict
reporting, the /ask pending-reply scenario, null-versus-omitted argument
semantics, name collisions across path/query/body sources, and
auto-generation of tools for newly added routes.

Reverse-proxy behaviour (§6.8) and the live ChatGPT connection (§6.9) are
verified manually; the proxy-friendly response headers are asserted here.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kuzya.api import create_app
from kuzya.api.mcp import attach_mcp_endpoint

TOKEN = "mcp-test-token"
TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Drive the ASGI lifespan protocol of ``app`` in-process."""
    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(app(scope, received.get, sent.put))
    await received.put({"type": "lifespan.startup"})
    message = await sent.get()
    if message["type"] != "lifespan.startup.complete":
        await task
        raise RuntimeError(f"application startup failed: {message}")
    try:
        yield
    finally:
        await received.put({"type": "lifespan.shutdown"})
        await sent.get()
        await task


def make_http_client(app: FastAPI, token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers=headers,
    )


@asynccontextmanager
async def mcp_server(
    migrated_db: pathlib.Path,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, ClientSession]]:
    """A started application with an authenticated MCP client session."""
    app = create_app(f"sqlite:///{migrated_db}", api_tokens={TOKEN})
    async with app_lifespan(app):
        client = make_http_client(app, TOKEN)
        try:
            async with streamable_http_client(
                "http://testserver/mcp", http_client=client
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield app, client, session
        finally:
            await client.aclose()


async def tool_name(session: ClientSession, name: str) -> str:
    """The exact registered name containing ``name``; fails if ambiguous."""
    tools = await session.list_tools()
    matches = [t.name for t in tools.tools if name in t.name]
    assert len(matches) == 1, f"expected one match for {name!r}, got {matches}"
    return matches[0]


# §6.1 — initialization and tool list -------------------------------------


async def test_handshake_and_tool_list_match_business_endpoints(
    migrated_db: pathlib.Path,
) -> None:
    app = create_app(f"sqlite:///{migrated_db}", api_tokens={TOKEN})
    openapi_paths = app.openapi()["paths"]
    async with app_lifespan(app):
        client = make_http_client(app, TOKEN)
        try:
            async with streamable_http_client(
                "http://testserver/mcp", http_client=client
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    assert init.server_info.name == "kuzya"
                    tools = (await session.list_tools()).tools
        finally:
            await client.aclose()

    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)), "tool names must be unique"
    assert all(TOOL_NAME_PATTERN.match(name) for name in names)
    # Every business operation of the OpenAPI document is covered, minus
    # /health and the multipart attachment upload (non-JSON request body).
    expected = 0
    for path, item in openapi_paths.items():
        if path in ("/health", "/mcp"):
            continue
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            content = (operation.get("requestBody") or {}).get("content") or {}
            if operation.get("requestBody") and "application/json" not in content:
                continue
            expected += 1
    assert len(names) == expected
    by_name = {tool.name: tool for tool in tools}
    assert "get_task_tasks__task_id__get" in by_name
    assert "create_task_tasks_post" in by_name
    assert not any("health" in name for name in names)
    get_tool = by_name["get_task_tasks__task_id__get"]
    assert get_tool.annotations is not None
    assert get_tool.annotations.read_only_hint is True
    create_tool = by_name["create_task_tasks_post"]
    assert create_tool.annotations is not None
    assert create_tool.annotations.read_only_hint is False
    # Argument grouping and inlined component schemas.
    assert set(get_tool.input_schema["properties"]) == {"path"}
    assert set(create_tool.input_schema["properties"]) == {"body"}
    assert "TaskCreate" in create_tool.input_schema.get("$defs", {})
    create_json = json.dumps(create_tool.input_schema)
    assert "#/components/" not in create_json


# §6.2 — authentication ----------------------------------------------------


async def test_mcp_requires_bearer_token(migrated_db: pathlib.Path) -> None:
    app = create_app(f"sqlite:///{migrated_db}", api_tokens={TOKEN})
    async with app_lifespan(app):
        for headers in ({}, {"Authorization": "Bearer wrong-token"}):
            async with make_http_client(app) as client:
                response = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "probe", "version": "0"},
                        },
                    },
                )
            assert response.status_code == 401


async def test_valid_token_handshake_succeeds(migrated_db: pathlib.Path) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        tools = await session.list_tools()
        assert tools.tools


# §6.3 — reads --------------------------------------------------------------


async def test_read_task_list_and_single_task(migrated_db: pathlib.Path) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        created = await client.post("/tasks", json={"title": "Read via MCP"})
        assert created.status_code == 201
        task_id = created.json()["id"]

        list_name = await tool_name(session, "list_tasks")
        listing = await session.call_tool(list_name, {"query": {"limit": 10}})
        assert listing.is_error is False
        # List responses arrive wrapped because structuredContent is an object.
        tasks = listing.structured_content["items"]
        assert isinstance(tasks, list)
        assert any(task["id"] == task_id for task in tasks)

        get_name = await tool_name(session, "get_task_tasks")
        fetched = await session.call_tool(get_name, {"path": {"task_id": task_id}})
        assert fetched.is_error is False
        assert fetched.structured_content["title"] == "Read via MCP"


# §6.4 — modification and transactions --------------------------------------


async def test_create_and_update_task_via_mcp(migrated_db: pathlib.Path) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        create_name = await tool_name(session, "create_task")
        created = await session.call_tool(
            create_name, {"body": {"title": "Created via MCP"}}
        )
        assert created.is_error is False
        assert created.structured_content["title"] == "Created via MCP"
        task_id = created.structured_content["id"]
        # Follow-up hint points at the matching GET tool.
        assert "get_task_tasks__task_id__get" in created.content[0].text

        verified = await client.get(f"/tasks/{task_id}")
        assert verified.status_code == 200
        assert verified.json()["title"] == "Created via MCP"

        patch_name = await tool_name(session, "patch_task")
        patched = await session.call_tool(
            patch_name,
            {
                "path": {"task_id": task_id},
                "body": {"status": "backlog", "description": "updated"},
            },
        )
        assert patched.is_error is False
        assert patched.structured_content["status"] == "backlog"
        assert patched.structured_content["description"] == "updated"


# §6.5 — errors, validation, conflicts, long operations --------------------


async def test_invalid_arguments_report_422_with_fields(
    migrated_db: pathlib.Path,
) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        create_name = await tool_name(session, "create_task")
        result = await session.call_tool(create_name, {"body": {}})
        assert result.is_error is True
        assert result.structured_content["http_status"] == 422
        assert "title" in result.content[0].text


async def test_missing_task_reports_404(migrated_db: pathlib.Path) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        get_name = await tool_name(session, "get_task_tasks")
        result = await session.call_tool(
            get_name, {"path": {"task_id": "does-not-exist"}}
        )
        assert result.is_error is True
        assert result.structured_content["http_status"] == 404


async def test_duplicate_task_id_reports_409(migrated_db: pathlib.Path) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        create_name = await tool_name(session, "create_task")
        first = await session.call_tool(
            create_name, {"body": {"id": "mcp-dup", "title": "one"}}
        )
        assert first.is_error is False
        second = await session.call_tool(
            create_name, {"body": {"id": "mcp-dup", "title": "two"}}
        )
        assert second.is_error is True
        assert second.structured_content["http_status"] == 409
        assert "mcp-dup" in second.content[0].text
        stored = await client.get("/tasks/mcp-dup")
        assert stored.json()["title"] == "one"


async def test_ask_timeout_reports_pending_reply_not_failure(
    migrated_db: pathlib.Path,
) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        ask_name = await tool_name(session, "ask_agent_ask")
        result = await session.call_tool(
            ask_name,
            {"body": {"message": "status check", "timeout_seconds": 0.1}},
        )
        # No secretary worker runs in this test, so the turn stays queued:
        # creation succeeded, only the reply is pending.
        assert result.is_error is False
        assert result.structured_content["status"] == "timeout"
        conversation_id = result.structured_content["conversation_id"]
        text = result.content[0].text
        assert "NOT a failure" in text
        assert "do not create the dialog again" in text
        messages_tool = await tool_name(session, "list_messages")
        assert messages_tool in text
        assert conversation_id in text
        # The dialog really exists with the user message appended.
        messages = await client.get(f"/conversations/{conversation_id}/messages")
        assert messages.status_code == 200
        assert any(m["role"] == "user" for m in messages.json())


# §6.6 — types, null handling, collisions -----------------------------------


async def test_explicit_null_differs_from_omitted_field(
    migrated_db: pathlib.Path,
) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        create_name = await tool_name(session, "create_task")
        created = await session.call_tool(
            create_name,
            {"body": {"title": "null-check", "description": "before"}},
        )
        task_id = created.structured_content["id"]
        patch_name = await tool_name(session, "patch_task")

        nulled = await session.call_tool(
            patch_name,
            {"path": {"task_id": task_id}, "body": {"description": None}},
        )
        assert nulled.is_error is False
        assert nulled.structured_content["description"] is None

        omitted = await session.call_tool(
            patch_name,
            {"path": {"task_id": task_id}, "body": {"status": "backlog"}},
        )
        assert omitted.is_error is False
        # The omitted description kept its explicit null value.
        assert omitted.structured_content["description"] is None
        assert omitted.structured_content["status"] == "backlog"


async def test_nested_structures_round_trip(migrated_db: pathlib.Path) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        create_name = await tool_name(session, "create_task")
        payload = {
            "title": "nested",
            "metadata": {"tags": ["a", "b"], "nested": {"deep": {"n": 1}}},
            "attachments": [{"name": "note", "path": "/inbox/note.txt"}],
        }
        created = await session.call_tool(create_name, {"body": payload})
        assert created.is_error is False
        stored = await client.get(f"/tasks/{created.structured_content['id']}")
        metadata = stored.json()["metadata"]
        assert metadata["tags"] == payload["metadata"]["tags"]
        assert metadata["nested"] == payload["metadata"]["nested"]
        # Attachments are persisted in metadata and surfaced as records.
        [attachment] = stored.json()["attachments"]
        assert attachment["name"] == "note"
        assert attachment["path"] == "/inbox/note.txt"


async def test_path_query_body_groups_stay_separate(
    migrated_db: pathlib.Path,
) -> None:
    app = create_app(f"sqlite:///{migrated_db}", api_tokens={TOKEN})
    registry = app.state.mcp_endpoint.registry
    patch_tool = registry.find("PATCH", "/tasks/{task_id}")
    assert patch_tool is not None
    # Same logical name in path and body namespaces never collides because
    # the groups are separate objects.
    assert set(patch_tool.input_schema["properties"]) == {"path", "body"}
    assert "task_id" in patch_tool.input_schema["properties"]["path"]["properties"]
    body = patch_tool.input_schema["properties"]["body"]
    body_fields = set(body.get("properties", {}).keys()) | set(
        patch_tool.input_schema.get("$defs", {}).get("TaskPatch", {}).get("properties", {}).keys()
    )
    assert "status" in body_fields

    list_tool = registry.find("GET", "/tasks")
    assert list_tool is not None
    assert set(list_tool.input_schema["properties"]) == {"query"}


# §6.7 — auto-generation after a restart -------------------------------------


async def test_new_route_becomes_a_tool_without_manual_registration() -> None:
    app = FastAPI()

    @app.get("/widgets/{widget_id}")
    def read_widget(widget_id: str, verbose: bool = False) -> dict[str, Any]:
        return {"widget_id": widget_id, "verbose": verbose}

    attach_mcp_endpoint(app)
    async with app_lifespan(app):
        # A plain FastAPI app has no custom lifespan of its own, so the
        # endpoint's context manager is entered here explicitly, exactly the
        # way create_app's lifespan does it for the real application.
        async with app.state.mcp_endpoint:
            client = make_http_client(app)
            try:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=client
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        names = [t.name for t in tools.tools]
                        assert any("widget" in name for name in names)
                        widget_tool = next(
                            t for t in tools.tools if "widget" in t.name
                        )
                        result = await session.call_tool(
                            widget_tool.name,
                            {
                                "path": {"widget_id": "w-1"},
                                "query": {"verbose": True},
                            },
                        )
                        assert result.is_error is False
                        assert result.structured_content == {
                            "widget_id": "w-1",
                            "verbose": True,
                        }
            finally:
                await client.aclose()


# §2.1 — transport behaviour behind proxies ---------------------------------


async def test_openapi_document_is_not_mutated_and_shared_components_resolve(
    migrated_db: pathlib.Path,
) -> None:
    def iter_refs(node: Any) -> AsyncIterator[str]:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                yield ref
            for value in node.values():
                yield from iter_refs(value)
        elif isinstance(node, list):
            for item in node:
                yield from iter_refs(item)

    app = create_app(f"sqlite:///{migrated_db}", api_tokens={TOKEN})
    # Tool generation ran inside create_app; the cached application
    # document must keep its original component references.
    document = json.dumps(app.openapi())
    assert "#/components/schemas/TaskAttachment" in document
    assert "#/$defs/" not in document

    registry = app.state.mcp_endpoint.registry
    sharers = []
    for tool in registry.tools:
        defs = tool.input_schema.get("$defs", {})
        for ref in iter_refs(tool.input_schema):
            assert ref.startswith("#/$defs/"), (tool.name, ref)
            assert ref[len("#/$defs/") :] in defs, (tool.name, ref)
        if "TaskAttachment" in defs:
            sharers.append(tool.name)
    # A nested component used by several endpoints resolves in every tool
    # that references it, not only in the first generated one.
    assert len(sharers) >= 2, sharers


async def test_slow_endpoint_tool_times_out_with_unknown_outcome() -> None:
    app = FastAPI()

    @app.get("/slow")
    async def slow() -> dict[str, bool]:
        await asyncio.sleep(0.5)
        return {"ok": True}

    attach_mcp_endpoint(app, timeout_seconds=0.1)
    async with app_lifespan(app):
        async with app.state.mcp_endpoint:
            client = make_http_client(app)
            try:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=client
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        slow_tool = next(
                            t
                            for t in (await session.list_tools()).tools
                            if "slow" in t.name
                        )
                        started = time.monotonic()
                        result = await session.call_tool(slow_tool.name, {})
                        elapsed = time.monotonic() - started
            finally:
                await client.aclose()
    assert result.is_error is True
    text = result.content[0].text
    assert "timed out" in text
    assert "UNKNOWN" in text
    assert "Do not repeat" in text
    assert elapsed < 0.4


async def test_timeout_hint_names_read_tool_and_id_from_arguments() -> None:
    app = FastAPI()

    @app.get("/tasks/{task_id}")
    def read_task(task_id: str) -> dict[str, str]:
        return {"task_id": task_id, "status": "unknown"}

    @app.patch("/tasks/{task_id}")
    async def update_task(task_id: str) -> dict[str, str]:
        await asyncio.sleep(0.5)
        return {"task_id": task_id, "status": "updated"}

    attach_mcp_endpoint(app, timeout_seconds=0.1)
    async with app_lifespan(app):
        async with app.state.mcp_endpoint:
            client = make_http_client(app)
            try:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=client
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = (await session.list_tools()).tools
                        patch_tool = next(
                            t for t in tools if t.name.startswith("update_task")
                        )
                        result = await session.call_tool(
                            patch_tool.name,
                            {"path": {"task_id": "known-task-123"}},
                        )
            finally:
                await client.aclose()
    assert result.is_error is True
    text = result.content[0].text
    # The read twin of the same endpoint plus the caller's own id give the
    # agent a concrete way to verify the unknown outcome.
    read_tool_name = next(
        t.name for t in tools if t.name.startswith("read_task")
    )
    assert read_tool_name in text
    assert "known-task-123" in text
    assert json.dumps({"path": {"task_id": "known-task-123"}}) in text
    assert "UNKNOWN" in text


async def test_ask_returns_promptly_without_explicit_timeout(
    migrated_db: pathlib.Path,
) -> None:
    async with mcp_server(migrated_db) as (app, client, session):
        ask_name = await tool_name(session, "ask_agent_ask")
        started = time.monotonic()
        result = await session.call_tool(
            ask_name, {"body": {"message": "quick ask, no explicit wait"}}
        )
        elapsed = time.monotonic() - started
        # Without an explicit timeout_seconds the tool must not hold the
        # call for the API's 60-second default poll.
        assert result.is_error is False
        assert result.structured_content["status"] == "timeout"
        assert "NOT a failure" in result.content[0].text
        assert elapsed < 5.0


async def test_mcp_endpoint_serves_both_path_forms_with_proxy_headers(
    migrated_db: pathlib.Path,
) -> None:
    app = create_app(f"sqlite:///{migrated_db}", api_tokens={TOKEN})
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }
    async with app_lifespan(app):
        async with make_http_client(app, TOKEN) as client:
            for path in ("/mcp", "/mcp/"):
                response = await client.post(path, json=initialize)
                assert response.status_code == 200, (path, response.status_code)
                assert response.headers.get("x-accel-buffering") == "no"
                assert response.headers.get("cache-control", "").startswith(
                    "no-cache"
                )
