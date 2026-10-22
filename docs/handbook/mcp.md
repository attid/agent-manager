# MCP transport

`/mcp` is a transport adapter over the REST API, not a second API. It exposes
the same handlers, validation and authorization boundary through the MCP
Streamable HTTP protocol. The adapter does not add business rules and does not
add permissions.

Source: `src/kuzya/api/mcp/endpoint.py` (mounting and MCP server),
`src/kuzya/api/mcp/registry.py` (OpenAPI-to-tool generation),
`src/kuzya/api/mcp/executor.py` (in-process dispatch through the real API) and
`src/kuzya/api/app.py` (`create_app`, middleware and `attach_mcp_endpoint`).

## Surface

- The transport is a plain Starlette `Route` appended to
  `app.router.routes` at the end of `create_app`, after every REST route:
  `app.router.routes.append(Route(MCP_PATH, endpoint=route_wrapper))`
  (`endpoint.py`, `attach_mcp_endpoint`). Both `/mcp` and `/mcp/` are
  registered so a proxy never sees a trailing-slash redirect.
- It is not a Starlette `Mount` and has no sub-application middleware of its
  own. The application-wide middleware wraps it because it is a route inside
  the same router.
- The tool registry is generated from `app.openapi()` when
  `attach_mcp_endpoint` runs. It is a snapshot for the application instance:
  a route added to a running app becomes a tool on the next application
  construction/restart, not dynamically during the same process.
- `/mcp` is not an OpenAPI operation and therefore is not in
  `app.openapi()`. It is declared in `src/kuzya/capabilities.py` as explicit
  adapter method/path records (`GET`/`POST` × `/mcp`/`/mcp/`,
  `kind="adapter"`), not as OpenAPI endpoints and not as 48 duplicated MCP
  tools. `audit_capabilities` excludes adapter records from the OpenAPI route
  comparison, checks their paths against the mounted application router and
  confirms `resolve_requirement` still returns `None` for them. The
  human-readable adapter row is in
  [Capability inventory](capability-inventory.md#mcp-transport-adapter).

### MCP tools versus REST routes

In the current tree the OpenAPI document contains 50 method+path operations.
The registry contains 48 tools. The mapping is derived, not hand-written:

| REST operation | MCP tool | Mapping |
|---|---|---|
| Every JSON business operation | one generated tool | `operationId`, normalized to `^[a-zA-Z0-9_-]{1,64}$`; arguments grouped as `path`, `query`, `body`. |
| `GET /tasks/{task_id}` | `get_task_tasks__task_id__get` | path parameter `task_id` inside `path`. |
| `POST /tasks` | `create_task_tasks_post` | JSON body inside `body`. |
| `GET /health` | none | listed in `EXCLUDED_PATHS`: service route, not a business operation. |
| `POST /tasks/{task_id}/attachments` | none | multipart request body; a JSON tool call cannot carry a binary upload. |
| `/mcp` | none | excluded from its own registry. |

The generator copies the declared JSON schemas, required flags, types,
formats, enums and bounds without adding restrictions. `$ref` pointers into
`#/components/schemas` are inlined into a self-contained `$defs` block.
Annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) are derived
from the HTTP method and path semantics, with optional per-route
`x-mcp-annotations` overrides.

Consequently "MCP repeats REST 1:1" is accurate for **all JSON business
operations**, but not literally for every REST route: health is a service
route and the multipart upload has no JSON representation.

## Middleware stack around `/mcp`

The effective call order is:

```
UnhandledExceptionMiddleware              (app.py)
  RequestCorrelationMiddleware            (app.py)
    BearerAuthMiddleware                  (app.py)
      FastAPI router
        Route("/mcp", _ProxyFriendlyASGI)  (endpoint.py)
          StreamableHTTPSessionManager
            MCP Server (tools/list, tools/call)
              ToolCallExecutor
                httpx.AsyncClient(ASGITransport(app=app))
                  -> re-enters this stack with the REST method/path
```

`create_app` calls `add_middleware` in this order: `BearerAuthMiddleware`,
then `RequestCorrelationMiddleware`, then `UnhandledExceptionMiddleware`
(`app.py`, `create_app`). Starlette inserts each new middleware at the front,
which produces the outer-to-inner order above. `attach_mcp_endpoint(app)` is
the last statement of `create_app`, after all API routes are registered.

`_ProxyFriendlyASGI` only adds `X-Accel-Buffering: no` and `Cache-Control:
no-cache` to responses; it performs no authentication and no routing.

Because the MCP route is appended to the same router that the middleware stack
wraps, there is no "middleware outside the mount" gap. The route is matched
after `BearerAuthMiddleware` has already accepted or rejected the HTTP
request.

Transport specifics:

- DNS-rebinding protection is disabled in
  `StreamableHTTPSessionManager` (`enable_dns_rebinding_protection=False`)
  because every request is already Bearer-authenticated.
- There is no MCP-specific CORS configuration, rate limiter, separate token or
  configuration switch that disables the endpoint.
- The only MCP-specific setting is
  `KUZYA_MCP_TOOL_TIMEOUT` (seconds, default `55`).

## Authentication and permissions

`BearerAuthMiddleware` runs on every HTTP request to `/mcp`, including later
requests of an already established MCP session. A session id does not carry
credentials; the `Authorization` header is checked again on each call.

| Caller | `/mcp` HTTP request | Tool dispatch |
|---|---|---|
| Missing or unknown token | `401 Unauthorized` | Never reached. |
| Administrative token (`KUZYA_API_TOKENS` / `KUZYA_API_TOKEN`) | Allowed, with no execution grant. | The same token is forwarded in-process; the inner REST request is also administrative. |
| Execution token from `ExecutionPermissions` | `403 Forbidden` | Not reached. `/mcp` has no route rule, so `resolve_requirement(method, "/mcp")` returns `None`. |
| Open development mode (no administrative token and no permission registry) | Allowed, like the rest of the API. | Inner REST request is also unauthenticated. |

The execution-token row is deliberately restrictive. `resolve_requirement`
(`src/kuzya/permissions.py`) knows the business `_ROUTE_RULES`, the deferred
`/operations` and subscription routes, and `/health`. It does not know
`/mcp`; `BearerAuthMiddleware` treats "no requirement" as forbidden for an
execution credential
(`app.py`, `requirement = resolve_requirement(method, path)` followed by the
`requirement is None` denial). An execution token therefore cannot use the
MCP transport at all. It does not gain a bypass; it loses a transport. Its
direct REST calls remain subject to the normal action/resource rules.

### Defense in depth on the inner call

A tool call is not executed by calling a Python handler directly. The
executor opens an `httpx.AsyncClient` with `ASGITransport(app=app)`
(`executor.py`, `ToolCallExecutor.start`) and forwards the caller's
`Authorization` header to the real REST path (`executor.py`,
`ToolCallExecutor.execute`). The request re-enters the application and is
authorized again, this time against the concrete REST method and path.

For a grant with `tasks.read` on `task:A`:

| Inner request | Result |
|---|---|
| `GET /tasks/A` | Allowed. |
| `GET /tasks/B` | `403`, resource does not match. |
| `GET /tasks` | `403`, a collection route requires wildcard resource authority. |
| `POST /tasks` | `403`, the grant lacks `tasks.write`. |

This means the outer `/mcp` denial is the first gate and the REST middleware
is the second. If the outer rule ever changes, the inner call still enforces
the same action/resource policy as a raw HTTP call. The transport cannot
widen an execution grant.

## What MCP adds and does not add

Adds:

- MCP tool discovery (`tools/list`) and JSON-RPC dispatch (`tools/call`).
- Argument grouping and name normalization suitable for MCP clients.
- Structured tool results: text plus `structuredContent`, with HTTP status
  and the API error body preserved.
- Streaming HTTP compatibility headers for reverse proxies.
- A default fast return for `/ask` (about one second) so a pending secretary
  reply is reported as pending, not as a failed creation.

Does not add:

- A second permission model, new scopes, OAuth, or a separate credential.
- Any route, handler, business rule or schema restriction beyond the REST
  contract.
- Access for execution tokens. `/mcp` is currently administrative/open-mode
  only.
- The multipart attachment upload; it is intentionally absent from the tool
  registry.

## Differences from a direct HTTP call

| Aspect | Direct REST | Through MCP |
|---|---|---|
| Protocol | HTTP method + path. | MCP Streamable HTTP with JSON-RPC `tools/call`. |
| Discovery | OpenAPI document. | `tools/list` generated from OpenAPI at application construction. |
| Arguments | Path, query and JSON body separately. | One object with `path`, `query`, `body` groups. |
| Authentication | `Authorization: Bearer ...`. | Same header, checked on every HTTP request and forwarded to the inner REST call. |
| Authorization | `BearerAuthMiddleware` + `resolve_requirement`. | Same middleware for `/mcp`; the inner REST call is authorized again. |
| Validation and errors | FastAPI request validation and HTTP status codes. | Same, wrapped as `CallToolResult` with `isError=true` and `structuredContent.http_status`. |
| Timeout | Client/proxy choice. | `KUZYA_MCP_TOOL_TIMEOUT`, default 55 s; an unknown mutating outcome is not retried. |
| Payload limits | Any content type. | JSON operations only; multipart upload is skipped. |
| Proxy behaviour | Application headers. | Adds no-buffering/no-cache headers for SSE. |

The direct REST endpoint and the generated MCP tool for the same operation
share one handler and one authorization decision. They differ in
representation, not in authority.

## How to verify

### Tool coverage

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from kuzya.api.app import create_app
from kuzya.api.mcp.registry import build_tool_registry

app = create_app("sqlite:////tmp/probe.db")
spec = app.openapi()
rest = {
    (method.upper(), path)
    for path, item in spec["paths"].items()
    for method in item
    if method in {"get", "post", "put", "patch", "delete"}
}
registry = build_tool_registry(spec)
tools = {(tool.method, tool.path) for tool in registry.tools}
print("REST operations:", len(rest))
print("MCP tools:", len(tools))
print("REST without MCP:", sorted(rest - tools))
print("/mcp in OpenAPI:", "/mcp" in spec["paths"])
PY
```

Expected in the current tree: `50` REST operations, `48` MCP tools,
`REST without MCP` containing `GET /health` and
`POST /tasks/{task_id}/attachments`, and `/mcp in OpenAPI: False`. The
inventory check also mounts the application and verifies the four adapter
records against `app.router.routes`; `/mcp` must be absent from OpenAPI and
absent from permission route rules.

### Authentication

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_mcp.py::test_mcp_requires_bearer_token \
  tests/test_mcp.py::test_valid_token_handshake_succeeds -q
```

For an execution token, build an app with a supplied `ExecutionPermissions`
registry, issue a grant (for example `tasks.read` on one task), and POST an
MCP `initialize` request with that grant's token. Expected: `403` from
`BearerAuthMiddleware`, because `/mcp` has no requirement. Then call the
executor directly with the same token for an allowed task, another task, and
`GET /tasks`. Expected: the allowed task succeeds; the other task and the
collection return `403` from the inner REST middleware.

## Invariant and residual risks

Verified invariant: **the MCP transport adds no authority**. It is ensured by
the route being inside the application middleware stack, by `/mcp` having no
entry in `_ROUTE_RULES` (so execution tokens are denied), and by every tool
call being re-authorised by the real REST route.

Residual risks:

- The invariant relies on `/mcp` staying out of permissive route rules. Adding
  a broad `/mcp` rule would change the outer gate; the inner REST re-check
  would still enforce the concrete action and resource.
- Administrative tokens remain unrestricted by design; MCP does not narrow
  them.
- There is no MCP-specific rate limit. Reverse-proxy limits, if any, are
  outside this repository.
- DNS-rebinding protection is disabled at the MCP transport layer because
  Bearer authentication is mandatory whenever authentication is enabled.
- The tool registry is a snapshot at application construction; a running
  process does not pick up newly added REST routes until it is rebuilt.

## Related

- [Tools and API discovery](tools-and-api.md)
- [Capability inventory](capability-inventory.md#mcp-transport-adapter)
- [Errors, security and isolation](errors-security-isolation.md)
- `docs/mcp-chatgpt.md` for the ChatGPT connection procedure.
