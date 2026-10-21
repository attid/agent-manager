"""Execute MCP tool calls against the FastAPI application in-process.

Tool calls are dispatched through ``httpx.AsyncClient`` with an
``ASGITransport`` bound to the same application object, so routing,
middleware, dependencies and validation of the real API run on every call
without network overhead. The caller's original ``Authorization`` header is
forwarded: the token never enters tool arguments, schemas or results, and
scoped execution credentials keep exactly their own permissions.

Error results are structured: the HTTP status code and the API error body
travel in ``structuredContent`` while the text explains what happened and
how to proceed. A timed-out mutating call reports an unknown outcome and is
never retried here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI
from mcp.types import CallToolResult, TextContent

from agent_manager.api.mcp.registry import GeneratedTool, ToolRegistry

logger = logging.getLogger(__name__)

#: Body texts are truncated to this many characters in TextContent; the full
#: payload stays available as structured data for JSON responses.
_TEXT_PREVIEW_LIMIT = 4000

_PATH_PARAM_PATTERN = re.compile(r"\{([a-zA-Z0-9_]+)\}")

#: Creating endpoint -> (GET endpoint reading the entity, response field
#: holding the new id, path parameter name of that GET endpoint).
_ENTITY_FOLLOW_UPS: dict[str, tuple[str, str, str, str]] = {
    "POST /tasks": ("GET", "/tasks/{task_id}", "id", "task_id"),
    "POST /conversations": (
        "GET",
        "/conversations/{conversation_id}",
        "id",
        "conversation_id",
    ),
    "POST /operations": ("GET", "/operations/{operation_id}", "id", "operation_id"),
    "POST /agents": ("GET", "/agents/{agent_id}", "agent_id", "agent_id"),
    "POST /conversations/{conversation_id}/messages": (
        "GET",
        "/conversations/{conversation_id}/messages",
        "conversation_id",
        "conversation_id",
    ),
    "POST /conversations/{conversation_id}/turns": (
        "GET",
        "/turns/{turn_id}",
        "id",
        "turn_id",
    ),
}

#: Cross-entity references in a response -> GET endpoint reading that
#: entity. Unlike the bare ``id`` field these names identify their entity
#: type, so they are safe to scan for in every successful dict response.
_ID_FOLLOW_UPS: dict[str, tuple[str, str]] = {
    "task_id": ("GET", "/tasks/{task_id}"),
    "conversation_id": ("GET", "/conversations/{conversation_id}"),
    "turn_id": ("GET", "/turns/{turn_id}"),
}

#: Endpoints whose reply-pending state is a normal outcome, not a failure.
_ASK_PATHS = frozenset({"/ask", "/conversations/ask"})
_ASK_MESSAGES_ENDPOINT = ("GET", "/conversations/{conversation_id}/messages")

#: Server-side wait imposed on /ask calls that did not pass an explicit
#: ``timeout_seconds``: the API default is a 60-second poll, which would hold
#: the tool call long past any proxy or client deadline. The API handler
#: floors the wait at 1.0s, so this is the shortest honest value.
_ASK_FAST_WAIT_SECONDS = 1.0


def _truncate(text: str) -> str:
    if len(text) <= _TEXT_PREVIEW_LIMIT:
        return text
    return text[:_TEXT_PREVIEW_LIMIT] + "\n…[truncated]"


def _format_json_preview(data: Any) -> str:
    try:
        return _truncate(json.dumps(data, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return _truncate(str(data))


@dataclass(frozen=True)
class _RequestPlan:
    url: str
    params: dict[str, Any]
    json_body: Any | None
    send_body: bool


def _build_plan(tool: GeneratedTool, arguments: dict[str, Any]) -> _RequestPlan:
    path = tool.path
    for name, value in (arguments.get("path") or {}).items():
        path = path.replace(
            f"{{{name}}}", quote(str(value), safe="")
        )
    query: dict[str, Any] = {}
    for name, value in (arguments.get("query") or {}).items():
        if value is None:
            # An explicit null has no query-string equivalent; FastAPI would
            # otherwise receive the literal string "None".
            continue
        query[name] = value
    body = arguments.get("body")
    if (
        tool.path in _ASK_PATHS
        and isinstance(body, dict)
        and "timeout_seconds" not in body
    ):
        # Return promptly with the pending state instead of holding the
        # call for the API's 60-second default poll; a caller-provided
        # timeout_seconds is always passed through unchanged.
        body = {**body, "timeout_seconds": _ASK_FAST_WAIT_SECONDS}
    return _RequestPlan(
        url=path,
        params=query,
        json_body=body,
        send_body=tool.has_body,
    )


class ToolCallExecutor:
    """Dispatch MCP tool calls to the FastAPI application via ASGI."""

    def __init__(
        self,
        app: FastAPI,
        registry: ToolRegistry,
        *,
        timeout_seconds: float,
    ) -> None:
        self._app = app
        self._registry = registry
        self._timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    async def start(self) -> None:
        """Open the in-process ASGI client. Called once from the app lifespan."""
        if self._client is not None:
            return
        # No httpx timeout: ASGITransport does not enforce it. The deadline
        # is imposed by asyncio.wait_for in execute().
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app),
            base_url="http://agent-manager.internal",
        )

    async def close(self) -> None:
        """Close the in-process ASGI client together with the application."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def execute(
        self,
        tool: GeneratedTool,
        arguments: dict[str, Any],
        authorization_header: str | None,
    ) -> CallToolResult:
        if self._client is None:
            raise RuntimeError(
                "MCP tool executor is not started; the application lifespan "
                "must run before tool calls arrive."
            )
        plan = _build_plan(tool, arguments)
        headers: dict[str, str] = {}
        if authorization_header:
            headers["Authorization"] = authorization_header
        try:
            response = await asyncio.wait_for(
                self._client.request(
                    tool.method,
                    plan.url,
                    params=plan.params or None,
                    json=plan.json_body if plan.send_body else None,
                    headers=headers,
                ),
                timeout=self._timeout_seconds,
            )
        except (TimeoutError, httpx.TimeoutException):
            return self._timeout_result(tool, arguments)
        return self._response_result(tool, response)

    # -- result assembly ------------------------------------------------

    def _timeout_result(
        self,
        tool: GeneratedTool,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        follow_up = self._timeout_follow_up_hint(tool, arguments)
        lines = [
            f"Tool call to {tool.method} {tool.path} timed out after "
            f"{self._timeout_seconds:g}s. The outcome is UNKNOWN: the "
            "request may or may not have been applied.",
            "Do not repeat the call automatically. " + follow_up,
        ]
        return CallToolResult(
            content=[TextContent(type="text", text="\n\n".join(lines))],
            isError=True,
        )

    def _timeout_follow_up_hint(
        self,
        tool: GeneratedTool,
        arguments: dict[str, Any],
    ) -> str:
        """Name a concrete verification path for a timed-out call.

        No response arrived, so the ids can only come from what the caller
        already supplied. The read twin of the same endpoint shares the path
        template, which covers the common ``PATCH/DELETE /entity/{id}`` case
        and plain collection endpoints (``POST /tasks`` -> ``GET /tasks``).
        """
        check = self._registry.find("GET", tool.path)
        if check is not None and check.name != tool.name:
            param_names = _PATH_PARAM_PATTERN.findall(tool.path)
            path_args = arguments.get("path") or {}
            if not param_names:
                args_example = "{}"
            elif all(name in path_args for name in param_names):
                args_example = json.dumps(
                    {"path": {name: path_args[name] for name in param_names}}
                )
            else:
                args_example = None
            if args_example is not None:
                return (
                    "Check the current state with tool "
                    f"`{check.name}`, arguments: {args_example}."
                )
        return (
            "If the call may have created or changed an entity, locate it "
            "through the matching list or search tool before acting further."
        )

    def _response_result(
        self,
        tool: GeneratedTool,
        response: httpx.Response,
    ) -> CallToolResult:
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            try:
                data: Any = response.json()
            except ValueError:
                data = response.text
        else:
            data = response.text

        if response.is_success:
            return self._success_result(tool, response, data)
        return self._error_result(tool, response, data)

    def _success_result(
        self,
        tool: GeneratedTool,
        response: httpx.Response,
        data: Any,
    ) -> CallToolResult:
        structured: Any
        if isinstance(data, dict):
            structured = data
        elif isinstance(data, list):
            # structuredContent must be an object per the MCP schema.
            structured = {"items": data}
        elif isinstance(data, str):
            structured = {"text": data}
        else:
            structured = {"value": data}

        lines = [f"HTTP {response.status_code}: {tool.method} {tool.path}"]
        # A pending secretary reply is a normal outcome of /ask: the dialog
        # exists and must not be reported — or re-created — as a failure.
        if (
            tool.path in _ASK_PATHS
            and isinstance(data, dict)
            and data.get("status") == "timeout"
        ):
            conversation_id = data.get("conversation_id")
            check = self._registry.find(*_ASK_MESSAGES_ENDPOINT)
            lines.append(
                "The conversation was created and the secretary reply is "
                f"not ready yet (conversation_id={conversation_id}, "
                f"turn_id={data.get('turn_id')}). This is NOT a failure — "
                "do not create the dialog again."
            )
            if check is not None and conversation_id is not None:
                args_example = json.dumps(
                    {"path": {"conversation_id": conversation_id}}
                )
                lines.append(
                    "Check the reply with tool "
                    f"`{check.name}`, arguments: {args_example}."
                )
        elif isinstance(data, dict):
            hint = self._follow_up_hint(tool, data)
            if hint:
                lines.append(hint)
        if isinstance(data, (dict, list)):
            lines.append(_format_json_preview(data))
        elif isinstance(data, str):
            lines.append(_truncate(data))
        return CallToolResult(
            content=[TextContent(type="text", text="\n\n".join(lines))],
            structuredContent=structured,
        )

    def _error_result(
        self,
        tool: GeneratedTool,
        response: httpx.Response,
        data: Any,
    ) -> CallToolResult:
        detail = data
        if isinstance(data, dict):
            detail = data.get("detail", data)
        lines = [
            f"API request failed: HTTP {response.status_code} from "
            f"{tool.method} {tool.path}.",
        ]
        if response.status_code == 401:
            lines.append(
                "The Bearer token was rejected. The connection credential "
                "is invalid or expired."
            )
        elif response.status_code == 422:
            lines.append(
                "The request arguments did not pass API validation; fix the "
                "listed fields and call again."
            )
        elif response.status_code == 404:
            lines.append("The addressed entity does not exist. Check the id.")
        elif response.status_code == 409:
            lines.append(
                "The request conflicts with the current state (for example "
                "a stale revision or a running task). Re-read the entity "
                "and retry with fresh values."
            )
        lines.append(_format_json_preview(detail))
        structured: Any
        if isinstance(data, dict):
            structured = {"http_status": response.status_code, "response": data}
        else:
            structured = {"http_status": response.status_code, "response": str(data)}
        return CallToolResult(
            content=[TextContent(type="text", text="\n\n".join(lines))],
            structuredContent=structured,
            isError=True,
        )

    def _follow_up_hint(
        self,
        tool: GeneratedTool,
        data: dict[str, Any],
    ) -> str:
        if not isinstance(data, dict):
            return ""
        entity_entry = _ENTITY_FOLLOW_UPS.get(f"{tool.method} {tool.path}")
        if entity_entry is not None:
            method, path_template, value_field, param_name = entity_entry
            value = data.get(value_field)
            check = self._registry.find(method, path_template)
            if value is not None and check is not None:
                args_example = json.dumps({"path": {param_name: value}})
                return (
                    "Follow up with tool "
                    f"`{check.name}`, arguments: {args_example}."
                )
        for id_field, (method, path_template) in _ID_FOLLOW_UPS.items():
            value = data.get(id_field)
            if value is None:
                continue
            check = self._registry.find(method, path_template)
            if check is None:
                continue
            param_name = path_template.strip("/").split("/")[-1].strip("{}")
            args_example = json.dumps({"path": {param_name: value}})
            return (
                "Verify the current state with tool "
                f"`{check.name}`, arguments: {args_example}."
            )
        return ""
