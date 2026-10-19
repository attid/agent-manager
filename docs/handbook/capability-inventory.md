# Capability inventory

Snapshot of the committed platform at base commit `fc7a3f7`. This is a
documentation inventory, not the executable parity checker that T05 must
build. A "Status" of `partial` means the tool submits a durable request but
the executor that would complete it is not registered.

## Tool inventory (28 tools)

Source: `src/pi_runner/policy.mjs`. "Deferred" means the tool writes a session
receipt that is applied after the turn by
`PersistentConversationHandler` (`src/agent_manager/persistent_conversation.py`).

| Tool | Behavior | Backend / receipt | Result | Status |
|---|---|---|---|---|
| `attach_document` | Deferred | `/session/attachments.json` | `pending` | implemented |
| `create_agent` | API write | `POST /agents` | `applied` | implemented |
| `archive_agent` | Deferred | `/session/archived_agents.json` | text | implemented (see gaps) |
| `read_knowledge` | Local read | knowledge root | text | implemented |
| `ask_clarification` | Deferred | `/session/clarification.json` | text | implemented (see gaps) |
| `save_project_summary` | Deferred | `/session/saved_projects.json` | text | implemented (see gaps) |
| `openviking_search` | Direct HTTP | OpenViking search API | text | implemented (config gap) |
| `openviking_read` | Direct HTTP | OpenViking content API | text | implemented (config gap) |
| `list_tasks` | API read | `GET /tasks` | `ok` | implemented |
| `get_task` | API read | `GET /tasks/{id}` | `ok` | implemented |
| `resolve_workspace` | API read | `GET /workspaces/resolve` | `ok` | implemented |
| `create_task` | API write | preflight + `POST /tasks` | `applied` | implemented |
| `update_task` | API write | preflight + `PATCH /tasks/{id}` | `applied` | implemented |
| `get_task_logs` | API read | `GET /tasks/{id}` + format | text | implemented |
| `resume_task_session` | API write | `GET` + `PATCH /tasks/{id}` | `applied` | implemented |
| `list_agents` | API read | `GET /agents` | `ok` | implemented |
| `get_agent` | API read | `GET /agents/{id}` | `ok` | implemented |
| `update_agent` | API write | `GET` + `PATCH /agents/{id}` | `applied` | implemented |
| `list_skills` | Local read | `/skills` | text | implemented |
| `install_skill` | Deferred | `/session/install_skills.json` | text | implemented (see gaps) |
| `create_skill` | Deferred | `/session/created_skills.json` | text | implemented (see gaps) |
| `list_knowledge` | Local read | knowledge root | text | implemented |
| `write_knowledge` | Deferred | `/session/written_knowledge.json` | `pending` | implemented |
| `update_component_rules` | Deferred | `/session/written_knowledge.json` | text | implemented (see gaps) |
| `trigger_review` | Operation | `POST /operations` kind `review` | `pending` | **partial** (no executor) |
| `trigger_commit_push` | Operation | `POST /operations` kind `git.commit_push` | `pending` | implemented |
| `trigger_rollback` | Operation | `POST /operations` kind `git.rollback` | `pending` | implemented |
| `trigger_analysis` | Operation | `POST /operations` kind `analysis` | `pending` | **partial** (no executor) |

## Operation kinds and executors

Source: `src/agent_manager/operation_worker.py`,
`src/agent_manager/operation_handlers.py`.

| Kind | Registered executor | Effect |
|---|---|---|
| `git.commit_push` | `git_commit_push_handler` | `GitEngine.commit_and_push` / `commit_local`; marks task `done`. |
| `git.rollback` | `git_rollback_handler` | `GitEngine.rollback`; marks task `cancelled`. |
| `review` | none | Fails visibly with unknown-executor diagnostic. T03 pending. |
| `analysis` | none | Fails visibly with unknown-executor diagnostic. T04 pending. |

Executors run outside any database transaction. The registration seam is
`OperationWorker.register(kind, handler)`; `register_default_operation_handlers`
is the built-in registration point.

## REST inventory

Source: `src/agent_manager/api/app.py`. See
[Tools and API discovery](tools-and-api.md#rest-endpoints) for the full table
and status codes.

Groups: health; workspaces/resolve; tasks (create/read/list/patch/import);
conversations and messages; turns; operations (submit/list/read); agents
(create/read/list/patch/archive/history).

## Worker inventory

| Worker | Source | Claims | Effect |
|---|---|---|---|
| `QueueDispatcher` | `src/agent_manager/dispatcher.py` | pending turns | Runs secretary handler, stores reply. |
| `TaskExecutionWorker` | `src/agent_manager/task_worker.py` | `in_progress` tasks | Runs agent, internal review, updates status. |
| `OperationWorker` | `src/agent_manager/operation_worker.py` | queued operations | Dispatches to registered executors. |

## Known truthfulness gaps (T05 must address)

- `archive_agent`, `ask_clarification`, `save_project_summary`,
  `install_skill`, `create_skill`, `update_component_rules` return free-form
  text instead of a structured status object. A caller cannot programmatically
  distinguish `pending` from `failed`.
- `trigger_review` and `trigger_analysis` return a durable operation, but no
  executor exists to complete it. Submitting is not a review or analysis.
- Telegram `/analyze` prints hardcoded findings.
- Operation idempotency derives from `(kind, target, payload)`, which
  suppresses later distinct requests (T02I).
- `openviking_*` and the backend fallback in `policy.mjs` embed default
  addresses/tokens instead of requiring injected per-instance settings.
- Skills/knowledge are declarative exports; the freeze factory reports
  `skills_connected=false`, `knowledge_connected=false`.

## Interfaces T05 must recheck after integration

1. Every registered JS tool maps to a real API route or a documented local
   effect; no tool silently drops input.
2. Every advertised operation kind has a registered executor or a truthful,
   specific unavailable diagnostic.
3. The OpenAPI document and the tool list agree on params/status codes.
4. Executable parity tests exercise the real tools against a temporary HTTP
   backend.
5. Discovery (`llms.txt`, this handbook, injected prompt, profile tool lists)
   matches the executable surface.

## Related

- `docs/plans/platform-completion/T05.md`
- `tests/platform_tools_contract.mjs`,
  `tests/test_platform_tools_contract.py`
