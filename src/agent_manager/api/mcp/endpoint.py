"""Wire the MCP Streamable HTTP transport into the FastAPI application.

The attachment owns three collaborating pieces:

* the tool registry generated from ``app.openapi()``;
* a low-level MCP ``Server`` whose handlers serve the registry and dispatch
  calls through the in-process ASGI executor;
* the SDK's streamable HTTP session manager, exposed as an async context
  manager that the application lifespan must enter (mounted Starlette
  sub-app lifespans never run inside FastAPI, so the parent lifespan drives
  it through :class:`McpEndpoint`).

Authentication is not reimplemented here: the application's existing
``BearerAuthMiddleware`` wraps every ``/mcp`` request, including requests of
an established MCP session, so a valid Bearer token is checked on each HTTP
call. The caller's token is read from the MCP request context and forwarded
to internal API calls only — it never becomes part of tool metadata.
"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any

from fastapi import FastAPI
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from starlette.datastructures import MutableHeaders
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_manager.api.mcp.executor import ToolCallExecutor
from agent_manager.api.mcp.registry import ToolRegistry, build_tool_registry

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"

DEFAULT_TOOL_TIMEOUT_SECONDS = 55.0
_TIMEOUT_ENV = "AGENT_MANAGER_MCP_TOOL_TIMEOUT"

_TIMEOUT_UNKNOWN_OUTCOME = (
    "Tool execution failed with an internal error. The outcome of the "
    "request is unknown: do not retry the call automatically — verify the "
    "current state through a matching GET tool instead."
)


def _resolve_timeout(timeout_seconds: float | None) -> float:
    if timeout_seconds is not None:
        return float(timeout_seconds)
    raw = os.environ.get(_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                "Ignoring invalid %s=%r; using the default MCP tool timeout",
                _TIMEOUT_ENV,
                raw,
            )
    return DEFAULT_TOOL_TIMEOUT_SECONDS


class _ProxyFriendlyASGI:
    """Add reverse-proxy-friendly headers to every MCP HTTP response.

    The inner application is rebindable: the session manager may only run
    once per instance, so each application lifespan cycle binds a fresh
    transport while the registered routes keep pointing here.
    """

    def __init__(self) -> None:
        self._inner: ASGIApp | None = None

    def bind(self, inner: ASGIApp) -> None:
        self._inner = inner

    def unbind(self) -> None:
        self._inner = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._inner is None:
            if scope["type"] != "http":
                return
            response = Response("MCP endpoint is not running", status_code=503)
            await response(scope, receive, send)
            return
        inner = self._inner
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # Nginx/Traefik must not buffer the SSE event stream.
                headers.setdefault("X-Accel-Buffering", "no")
                headers.setdefault("Cache-Control", "no-cache")
            await send(message)

        await inner(scope, receive, send_with_headers)


class McpEndpoint:
    """Lifecycle owner of the mounted MCP server.

    Entering the context manager starts the streamable HTTP session manager
    and the in-process ASGI client; leaving it stops both. FastAPI never
    runs lifespans of mounted sub-applications, so ``create_app`` registers
    this object on ``app.state`` and the application lifespan enters it.

    A session manager may run only once per instance, so every entry builds
    a fresh transport and rebinds it into the mounted route wrapper. This
    keeps applications that stop and restart their lifespan working.
    """

    def __init__(
        self,
        *,
        app: FastAPI,
        registry: ToolRegistry,
        executor: ToolCallExecutor,
        server: Server[Any],
        route_wrapper: _ProxyFriendlyASGI,
    ) -> None:
        self._app = app
        self._registry = registry
        self._executor = executor
        self._server = server
        self._route_wrapper = route_wrapper
        self._stack: AsyncExitStack | None = None

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def executor(self) -> ToolCallExecutor:
        return self._executor

    def _create_transport(self) -> StreamableHTTPSessionManager:
        # DNS-rebinding protection would reject the proxied public Host
        # header; every /mcp request is already authenticated by
        # BearerAuthMiddleware.
        manager = StreamableHTTPSessionManager(
            app=self._server,
            stateless=False,
            json_response=False,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
        self._route_wrapper.bind(StreamableHTTPASGIApp(manager))
        return manager

    async def __aenter__(self) -> McpEndpoint:
        if self._stack is not None:
            return self
        self._stack = AsyncExitStack()
        try:
            manager = self._create_transport()
            await self._executor.start()
            await self._stack.enter_async_context(manager.run())
        except BaseException:
            self._route_wrapper.unbind()
            await self._stack.aclose()
            self._stack = None
            await self._executor.close()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()
        self._route_wrapper.unbind()
        await self._executor.close()


def _build_mcp_server(
    registry: ToolRegistry,
    executor: ToolCallExecutor,
) -> Server[Any]:
    async def handle_list_tools(
        ctx: Any,
        params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        tools = [
            Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_schema,
                annotations=ToolAnnotations(**tool.annotations),
            )
            for tool in registry.tools
        ]
        return ListToolsResult(tools=tools)

    async def handle_call_tool(
        ctx: Any,
        params: CallToolRequestParams,
    ) -> CallToolResult:
        tool = registry.by_name(params.name)
        if tool is None:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Unknown tool: {params.name}",
                    )
                ],
                isError=True,
            )
        authorization: str | None = None
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None:
            authorization = headers.get("authorization")
        try:
            return await executor.execute(
                tool,
                dict(params.arguments or {}),
                authorization,
            )
        except Exception:
            logger.exception(
                "MCP tool %s crashed while dispatching %s %s",
                params.name,
                tool.method,
                tool.path,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=_TIMEOUT_UNKNOWN_OUTCOME)],
                isError=True,
            )

    return Server(
        "agent-manager",
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


def attach_mcp_endpoint(
    app: FastAPI,
    *,
    timeout_seconds: float | None = None,
) -> McpEndpoint:
    """Mount the MCP endpoint on ``/mcp`` of an existing FastAPI app.

    Must be called after all API routes are registered: the tool registry is
    generated from ``app.openapi()`` at attachment time and refreshes on the
    next application start. The application lifespan must enter the returned
    endpoint (``create_app`` does this); a plain FastAPI app wires it into
    its own lifespan.
    """
    registry = build_tool_registry(app.openapi())
    executor = ToolCallExecutor(
        app,
        registry,
        timeout_seconds=_resolve_timeout(timeout_seconds),
    )
    server = _build_mcp_server(registry, executor)
    route_wrapper = _ProxyFriendlyASGI()
    app.router.routes.append(Route(MCP_PATH, endpoint=route_wrapper))
    # Register both forms so no client or proxy ever sees a trailing-slash
    # redirect for the protocol endpoint.
    app.router.routes.append(Route(MCP_PATH + "/", endpoint=route_wrapper))
    endpoint = McpEndpoint(
        app=app,
        registry=registry,
        executor=executor,
        server=server,
        route_wrapper=route_wrapper,
    )
    app.state.mcp_endpoint = endpoint
    logger.info(
        "MCP endpoint mounted at %s with %d generated tools",
        MCP_PATH,
        len(registry.tools),
    )
    return endpoint
