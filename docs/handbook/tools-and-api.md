# Tools and API discovery

The platform exposes one loopback REST API and one set of Pi JS tools. The JS
tools are only a client of the API (plus local knowledge/session files); they
do not implement domain logic. This file is the discovery contract.

## REST endpoints

Authoritative list is the generated OpenAPI document (`GET /openapi.json`).
Current surface (`src/agent_manager/api/app.py`):

| Method | Path | Success | Notes |
|---|---|---|---|
| `GET` | `/health` | `200` | Exempt from Bearer auth. |
| `GET` | `/workspaces/resolve` | `200`, `404`, `409`, `422` | Read-only execution preflight. |
| `POST` | `/tasks` | `201`, `409`, `422` | Create task. |
| `GET` | `/tasks` | `200` | Filters: `status`, `workspace`, `limit`, `offset`. |
| `GET` | `/tasks/{task_id}` | `200`, `404` | Read one task. |
| `PATCH` | `/tasks/{task_id}` | `200`, `404` | Partial update. |
| `POST` | `/imports/tasks` | `200` | Idempotent historical import. |
| `POST` | `/conversations` | `201`, `409` | Create conversation. |
| `GET` | `/conversations` | `200` | List. |
| `GET` | `/conversations/{id}` | `200`, `404` | Read. |
| `POST` | `/conversations/{id}/messages` | `201`/`200`, `404`, `409` | `201` insert, `200` idempotent replay. |
| `GET` | `/conversations/{id}/messages` | `200`, `404` | List messages. |
| `POST` | `/conversations/{id}/turns` | `201`, `400`, `404` | Enqueue secretary turn. |
| `GET` | `/turns/{turn_id}` | `200`, `404` | Turn status/reply reference. |
| `POST` | `/operations` | `202`/`200`, `409` | Submit deferred operation. |
| `GET` | `/operations` | `200` | Filters: `status`, `kind`, `target_id`, `limit`, `offset`. |
| `GET` | `/operations/{id}` | `200`, `404` | Read status/result/error. |
| `POST` | `/agents` | `201`, `409` | Create revision 1. |
| `GET` | `/agents` | `200` | `include_archived` query. |
| `GET` | `/agents/{id}` | `200`, `404` | Current snapshot. |
| `GET` | `/agents/{id}/revisions` | `200` | History (empty list for unknown id). |
| `GET` | `/agents/{id}/revisions/{revision}` | `200`, `404` | One snapshot. |
| `PATCH` | `/agents/{id}` | `200`, `404`, `409` | Append revision after optimistic check. |
| `DELETE` | `/agents/{id}` | `200`, `400`, `404` | Archive; core system agents rejected. |

## Authentication

`BearerAuthMiddleware` (`src/agent_manager/api/app.py`):

- If no tokens are configured (`AGENT_MANAGER_API_TOKENS` /
  `AGENT_MANAGER_API_TOKEN` empty), the API is open. This is the development
  mode.
- When tokens are configured, every path except `/health` and `OPTIONS`
  requires `Authorization: Bearer <token>`. Missing/invalid tokens return
  `401` with `WWW-Authenticate: Bearer`.

**Pending (T02I):** execution-scoped API tokens restricted to intended
actions and resources. In this snapshot a valid token grants the full API
surface.

## Error contract

- 4xx/5xx JSON body: `{"detail": "<message>"}`.
- An unhandled exception returns `500` with
  `{"detail": "Internal server error", "request_id": "..."}` and logs a full
  traceback. The process stays alive and the next request is served.
- Every response carries `X-Request-ID` (incoming value preserved, otherwise a
  fresh UUID). Use it to correlate logs.

## Idempotency

- `POST /conversations/{id}/messages`: `(conversation_id, source,
  external_id)`. A reused key with a different payload returns `409`.
- `POST /operations`: `idempotency_key`. Exact retry returns the stored
  operation (`200`); reused key with a different payload returns `409`.
- `POST /imports/tasks`: stable task `id`.
- `POST /conversations/{id}/turns`: unique `input_message_id`.
- `PATCH /agents/{id}`: `expected_revision` (not idempotent; it is an
  optimistic concurrency check).

**Known gap (T02I):** the JS tools currently derive the operation
`idempotency_key` as a deterministic hash of `(kind, target, payload)`
(`operationKey` in `src/pi_runner/policy.mjs`). That makes an exact retry
safe but suppresses a later, distinct intentional request with the same
payload forever. T02I changes this so only retries of one invocation share a
key and a new invocation can create a new operation.

## Registered JS tools

Defined in `src/pi_runner/policy.mjs`. There are 28 tools. Grouped by
behaviour:

**API-backed reads/writes** (return `ok`/`applied`/`failed`):

`list_tasks`, `get_task`, `get_task_logs`, `resolve_workspace`, `create_task`,
`update_task`, `resume_task_session`, `list_agents`, `get_agent`,
`update_agent`, `create_agent`.

**Durable operation submissions** (return the operation; inspect it to learn
the outcome): `trigger_review`, `trigger_analysis`, `trigger_commit_push`,
`trigger_rollback`.

**Session-deferred writes** (write a receipt file; applied after the turn):
`attach_document`, `archive_agent`, `save_project_summary`, `write_knowledge`,
`update_component_rules`, `install_skill`, `create_skill`,
`ask_clarification`, plus the legacy `created_agents.json` /
`archived_agents.json` / `actions.json` drains.

**Local reads**: `read_knowledge`, `list_knowledge`, `list_skills`.

**Direct integrations**: `openviking_search`, `openviking_read`.

See [Capability inventory](capability-inventory.md) for the per-tool mapping.

## Tool exposure

A model only sees the tools named in its `AgentProfile.runner_config.tools`.
The default secretary list lives in `src/agent_manager/knowledge.py`
(`sec_tools`). Changing a profile's `tools` array changes exposure on the next
execution; it does not grant API permissions by itself.

## Discovery commands

```bash
# Endpoints and status codes
PYTHONPATH=src .venv/bin/python -c \
  "from agent_manager.api.app import create_app; \
   app=create_app('sqlite:////tmp/probe.db'); \
   [print(m.upper(), p, sorted(app.openapi()['paths'][p][m]['responses'])) \
    for p in sorted(app.openapi()['paths']) \
    for m in sorted(app.openapi()['paths'][p])]"

# Tool names
grep -n 'name: "' src/pi_runner/policy.mjs

# Operation kinds and registrations
grep -n 'OPERATION_KIND_\|\.register(' \
  src/agent_manager/operation_worker.py \
  src/agent_manager/operation_handlers.py \
  src/agent_manager/runner.py
```

## Pending interfaces

- T02I: kind/target-scoped `/operations` authorization and scoped reads.
- T05: machine-readable capability inventory and executable parity checks
  against OpenAPI + registered tools; discovery injection for secretary and
  system agents.

## Related

- `src/agent_manager/api/app.py`, `src/agent_manager/api/schemas.py`
- `src/pi_runner/policy.mjs`
- `docs/api.md`, `docs/secretary-tools.md`
