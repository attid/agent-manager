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
| `POST` | `/tasks/{task_id}/attachments` | `201`, `401`, `403`, `404`, `422` | Authenticated multipart/form-data upload route; records file in task artifacts and metadata. |
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
| `GET` | `/tasks/{task_id}/analysis` | `200` | List a task's analysis reports. |
| `GET` | `/analysis/{report_id}` | `200`, `404` | Read one analysis report with evidence. |
| `GET` | `/analysis/{report_id}/recommendations` | `200`, `404` | List a report's recommendations. |
| `GET` | `/recommendations` | `200` | Filters: `task_id`, `status`, `target_kind`, `limit`, `offset`. |
| `GET` | `/recommendations/{id}` | `200`, `404` | Preview a recommendation and its conflict state. |
| `POST` | `/recommendations/{id}/approve` | `200`, `404`, `409` | Explicit approval; never mutates the target. |
| `POST` | `/recommendations/{id}/apply` | `200`, `404`, `409`, `422` | Apply after a revision/hash recheck. |
| `POST` | `/agents` | `201`, `409` | Create revision 1. |
| `GET` | `/agents` | `200` | `include_archived` query. |
| `GET` | `/agents/{id}` | `200`, `404` | Current snapshot. |
| `GET` | `/agents/{id}/revisions` | `200` | History (empty list for unknown id). |
| `GET` | `/agents/{id}/revisions/{revision}` | `200`, `404` | One snapshot. |
| `PATCH` | `/agents/{id}` | `200`, `404`, `409` | Append revision after optimistic check. |
| `DELETE` | `/agents/{id}` | `200`, `400`, `404` | Archive; core system agents rejected. |
| `GET` | `/knowledge` | `200` | List Markdown documents, optional `category`. |
| `GET` | `/knowledge/{path}` | `200`, `404` | Read one document. |
| `PUT` | `/knowledge/{path}` | `200`, `400` | Write/update one document and commit when git-backed. |
| `DELETE` | `/knowledge/{path}` | `200`, `400`, `404` | Delete one exact Markdown file; `agents/`, wildcards, directories, traversal and symlinks rejected. |

## Authentication

`BearerAuthMiddleware` (`src/agent_manager/api/app.py`) distinguishes two
credential classes:

- administrative tokens from `AGENT_MANAGER_API_TOKENS` /
  `AGENT_MANAGER_API_TOKEN` receive unrestricted access and are never handed to
  a child execution; and
- execution tokens issued by `ExecutionPermissions`
  (`src/agent_manager/permissions.py`) are accepted only inside their granted
  action/resource scope. `resolve_requirement` maps each route to a concrete
  action and resource, so a raw HTTP call from a shell cannot bypass the tool
  exposure policy.

The authenticated/`require_auth` decision is fixed at process start: issuing
or revoking execution grants never switches the instance between anonymous and
protected. A standalone app with no administrative token and no permission
registry stays in explicit open development mode. `/health` and `OPTIONS` are
public.

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

**Implemented:** the JS tools derive the operation `idempotency_key` from
`[kind, target_id, input, toolCallId]` (`operationKey` in
`src/pi_runner/policy.mjs`). An exact retry of one invocation reuses the same
operation; a later, distinct invocation with the same payload creates a new
operation. An explicit `idempotency_key` overrides this and is honored across
invocations.

## Registered JS tools

Defined in `src/pi_runner/policy.mjs`. There are 37 tools. Grouped by
behaviour:

**API-backed reads/writes** (return `ok`/`applied`/`failed`):

`list_tasks`, `get_task`, `get_task_logs`, `resolve_workspace`, `create_task`,
`update_task`, `resume_task_session`, `list_agents`, `get_agent`,
`update_agent`, `create_agent`, `delete_knowledge`. Note: `create_task` and `update_task` accept an optional typed `attachments` array to stage input files into `/inbox`. `delete_knowledge` removes exactly one explicitly confirmed `.md` file through `DELETE /knowledge/{path}` and never accepts `agents/`, wildcards or directories.

**Durable operation submissions** (return the operation; inspect it to learn
the outcome): `trigger_review`, `trigger_analysis`, `trigger_commit_push`,
`trigger_rollback`.

**Analysis and recommendation reads/writes** (return `ok`/`applied`/`failed`):
`list_analysis_reports`, `get_analysis_report`, `list_recommendations`,
`preview_recommendation`, `approve_recommendation`, `apply_recommendation`.
These are exposed to the secretary profile only, not added unconditionally to
every execution.

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

## Capability inventory

`src/agent_manager/capabilities.py` is the machine-readable inventory of every
tool and route (method, schema, result status, permission action and handbook
document). `audit_capabilities` compares it with the generated OpenAPI
document, the tools actually registered by `policy.mjs` and
`ACTIONS_FOR_TOOLS`. `tests/test_capabilities.py` runs that audit, so an
undeclared tool, unsupported route, schema drift or silent privilege widening
fails deterministically. The `doc` field links each capability to its handbook
file.

## Related

- `src/agent_manager/api/app.py`, `src/agent_manager/api/schemas.py`
- `src/pi_runner/policy.mjs`
- `docs/api.md`, `docs/secretary-tools.md`
