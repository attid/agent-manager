"""Generate the MCP tool registry from the application's OpenAPI document.

The registry is rebuilt when the application starts, so every FastAPI route
registered before :func:`attach_mcp_endpoint` runs automatically becomes an
MCP tool without a hand-written wrapper.

Rules (see ``docs/tasks/mcp-endpoint-spec.md``):

* Service and documentation routes are excluded, as is ``/mcp`` itself.
* Tool names are normalized operation ids capped at 64 characters and made
  unique; they must match ``^[a-zA-Z0-9_-]{1,64}$``.
* Tool arguments are grouped into ``path``, ``query`` and ``body`` objects so
  equally named parameters from different sources never collide.
* ``$ref`` pointers into ``#/components/schemas`` are inlined into a
  self-contained ``$defs`` block; recursive types keep internal references.
* No restrictions are added on top of the existing API: required flags,
  types, formats, enums and bounds are copied as declared.
* Tool annotations (``readOnlyHint`` etc.) are derived from real behaviour:
  HTTP method plus path semantics, with per-route refinements through the
  ``x-mcp-annotations`` OpenAPI extension.
* Operations whose request body is not JSON (the multipart attachment
  upload) are skipped: a JSON tool call cannot carry a binary file, and
  inventing filename/content semantics would not be the real API contract.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

TOOL_NAME_MAX_LENGTH = 64
_JSON_CONTENT_TYPE = "application/json"
_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}
_COMPONENTS_PREFIX = "#/components/schemas/"

#: Service, technical and documentation routes that are not business methods.
EXCLUDED_PATHS = frozenset(
    {
        "/health",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/mcp",
    }
)

#: Path segments that mark an operation as destructive regardless of method.
DESTRUCTIVE_SEGMENTS = frozenset({"cancel", "archive", "stop", "delete"})

_ANNOTATION_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint")

_DESCRIPTION_LIMIT = 4000


@dataclass(frozen=True)
class GeneratedTool:
    """One MCP tool mapped to a concrete OpenAPI operation."""

    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, bool]
    method: str
    path: str
    has_body: bool


class ToolRegistry:
    """The generated tools with name and endpoint lookups."""

    def __init__(self, tools: list[GeneratedTool]) -> None:
        self._tools = tuple(tools)
        self._by_name = {tool.name: tool for tool in self._tools}
        self._by_endpoint = {
            (tool.method, tool.path): tool for tool in self._tools
        }

    @property
    def tools(self) -> tuple[GeneratedTool, ...]:
        return self._tools

    def by_name(self, name: str) -> GeneratedTool | None:
        return self._by_name.get(name)

    def find(self, method: str, path: str) -> GeneratedTool | None:
        return self._by_endpoint.get((method.upper(), path))


def _normalize_tool_name(operation_id: str, used: set[str]) -> str:
    """Return a stable, unique, schema-compliant tool name."""
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", operation_id).strip("_") or "tool"
    if len(name) > TOOL_NAME_MAX_LENGTH:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:7]
        name = f"{name[: TOOL_NAME_MAX_LENGTH - 8]}_{digest}"
    candidate = name
    counter = 2
    while candidate in used:
        suffix = f"_{counter}"
        candidate = f"{name[: TOOL_NAME_MAX_LENGTH - len(suffix)]}{suffix}"
        counter += 1
    used.add(candidate)
    return candidate


def _fallback_operation_id(method: str, path: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
    return f"{method.lower()}_{slug}"


def _collect_defs(node: Any, components: Mapping[str, Any], defs: dict[str, Any]) -> None:
    """Recursively copy every referenced component schema into ``defs``.

    Components are deep-copied: the OpenAPI document is shared with the
    application (FastAPI caches it), and the in-place ``$ref`` rewrite below
    must never leak back into it or into a later tool built from the same
    components.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_COMPONENTS_PREFIX):
            name = ref[len(_COMPONENTS_PREFIX) :]
            if name not in defs and name in components:
                component = copy.deepcopy(components[name])
                defs[name] = component
                _collect_defs(component, components, defs)
        for value in node.values():
            _collect_defs(value, components, defs)
    elif isinstance(node, list):
        for item in node:
            _collect_defs(item, components, defs)


def _rewrite_refs(node: Any) -> None:
    """Rewrite ``#/components/schemas/X`` pointers to ``#/$defs/X`` in place."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_COMPONENTS_PREFIX):
            node["$ref"] = f"#/$defs/{ref[len(_COMPONENTS_PREFIX) :]}"
        for value in node.values():
            _rewrite_refs(value)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item)


def _params_object(params: list[Mapping[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in params:
        name = param.get("name")
        if not isinstance(name, str):
            continue
        schema = param.get("schema")
        if not isinstance(schema, dict):
            schema = {"type": "string"}
        properties[name] = copy.deepcopy(schema)
        if param.get("required"):
            required.append(name)
    obj: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        obj["required"] = required
    return obj


def _json_body_schema(operation: Mapping[str, Any]) -> tuple[dict[str, Any] | None, bool] | None:
    """Return the JSON body schema and its required flag.

    ``None`` means the operation must be skipped (non-JSON request body).
    ``(None, False)`` means the operation has no request body at all.
    """
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        return (None, False)
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return (None, False)
    json_content = content.get(_JSON_CONTENT_TYPE)
    if json_content is None:
        return None
    schema = json_content.get("schema")
    if not isinstance(schema, dict):
        return (None, False)
    return (copy.deepcopy(schema), bool(request_body.get("required")))


def _build_input_schema(
    path_params: list[Mapping[str, Any]],
    query_params: list[Mapping[str, Any]],
    body_schema: dict[str, Any] | None,
    body_required: bool,
    components: Mapping[str, Any],
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    if path_params:
        # Path parameters are always required by HTTP routing.
        properties["path"] = _params_object(path_params)
        required.append("path")
    if query_params:
        query_object = _params_object(query_params)
        properties["query"] = query_object
        if query_object.get("required"):
            required.append("query")
    if body_schema is not None:
        properties["body"] = body_schema
        if body_required:
            required.append("body")
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    defs: dict[str, Any] = {}
    _collect_defs(schema, components, defs)
    if defs:
        # Attach the definitions before rewriting so refs inside the
        # definition bodies are rewritten too.
        schema["$defs"] = defs
        _rewrite_refs(schema)
    return schema


def _build_annotations(
    method: str,
    path: str,
    operation: Mapping[str, Any],
) -> dict[str, bool]:
    read_only = method in ("GET", "HEAD")
    destructive = method == "DELETE" or any(
        segment in DESTRUCTIVE_SEGMENTS for segment in path.split("/")
    )
    idempotent = method in ("GET", "HEAD", "PUT")
    annotations = {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        # Tools only reach the loopback Kuzya API itself.
        "openWorldHint": False,
    }
    overrides = operation.get("x-mcp-annotations")
    if isinstance(overrides, Mapping):
        for key in _ANNOTATION_KEYS:
            if key in overrides:
                annotations[key] = bool(overrides[key])
    return annotations


def _side_effects_note(method: str, path: str) -> str | None:
    if method == "GET":
        return None
    if path in ("/ask", "/conversations/ask"):
        return (
            "Side effects: creates or appends to a conversation and enqueues a "
            "secretary turn. The tool returns promptly by default (about one "
            "second): a `timeout` status means the dialog exists and the "
            "reply is not ready yet — read the conversation messages instead "
            "of resending the question. Pass body.timeout_seconds to wait "
            "longer for the reply before returning."
        )
    if path == "/operations":
        return (
            "Side effects: creates a durable background operation. The HTTP "
            "response only confirms submission; check the returned "
            "operation_id via GET /operations/{operation_id}."
        )
    if path == "/tasks":
        return (
            "Side effects: creates a task. Execution is picked up by the "
            "background task worker; poll the task via GET /tasks/{task_id}."
        )
    if path.endswith("/apply") or path.endswith("/approve"):
        return (
            "Side effects: changes persisted state. Do not retry "
            "automatically if the outcome is unknown; verify the current "
            "state through the corresponding GET endpoint first."
        )
    return (
        f"Side effects: this HTTP {method} mutates API state. Do not retry "
        "the call automatically when the outcome is unknown; check the "
        "current state with the matching GET endpoint instead."
    )


def _build_description(
    method: str,
    path: str,
    operation: Mapping[str, Any],
) -> str:
    parts: list[str] = []
    summary = operation.get("summary")
    description = operation.get("description")
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    if isinstance(description, str) and description.strip() and description.strip() != summary:
        parts.append(description.strip())
    parts.append(f"Calls HTTP endpoint: {method} {path}")
    note = _side_effects_note(method, path)
    if note is not None:
        parts.append(note)
    parts.append(
        "Arguments are grouped: `path` holds path parameters, `query` holds "
        "query parameters and `body` holds the JSON request body. Omit "
        "groups that do not apply."
    )
    return "\n\n".join(parts)[:_DESCRIPTION_LIMIT]


def build_tool_registry(openapi_document: Mapping[str, Any]) -> ToolRegistry:
    """Generate the MCP tool registry from an OpenAPI document."""
    components = (openapi_document.get("components") or {}).get("schemas") or {}
    paths = openapi_document.get("paths") or {}
    used_names: set[str] = set()
    tools: list[GeneratedTool] = []

    for path, path_item in paths.items():
        if not isinstance(path_item, Mapping) or path in EXCLUDED_PATHS:
            continue
        shared_params = [
            param
            for param in path_item.get("parameters", [])
            if isinstance(param, Mapping)
        ]
        for method_lower in _HTTP_METHODS:
            operation = path_item.get(method_lower)
            if not isinstance(operation, Mapping):
                continue
            body = _json_body_schema(operation)
            if body is None:
                logger.info(
                    "MCP tool generation skipped %s %s: non-JSON request body",
                    method_lower.upper(),
                    path,
                )
                continue
            body_schema, body_required = body
            params = shared_params + [
                param
                for param in operation.get("parameters", [])
                if isinstance(param, Mapping)
            ]
            path_params = [p for p in params if p.get("in") == "path"]
            query_params = [p for p in params if p.get("in") == "query"]
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                operation_id = _fallback_operation_id(method_lower, path)
            method = method_lower.upper()
            tools.append(
                GeneratedTool(
                    name=_normalize_tool_name(operation_id, used_names),
                    description=_build_description(method, path, operation),
                    input_schema=_build_input_schema(
                        path_params,
                        query_params,
                        body_schema,
                        body_required,
                        components,
                    ),
                    annotations=_build_annotations(method, path, operation),
                    method=method,
                    path=path,
                    has_body=body_schema is not None,
                )
            )

    return ToolRegistry(tools)
