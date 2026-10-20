# Capability inventory

The authoritative machine-readable inventory is
`src/agent_manager/capabilities.py`. It declares every registered Pi JS tool
and REST endpoint with its method, schema, result status, permission action,
operation kind and handbook document. `audit_capabilities` compares that
declaration with the generated OpenAPI document, the real tools registered by
`src/pi_runner/policy.mjs` and `ACTIONS_FOR_TOOLS`; `tests/test_capabilities.py`
runs it. An undeclared tool, unsupported route, schema drift or silent
privilege widening fails the check.

## Tool inventory (34 tools)

Source: `src/pi_runner/policy.mjs`. "Deferred" means the tool writes a session
receipt that `PersistentConversationHandler` applies after the turn. A local
tool grants no backend action, so it cannot widen raw-HTTP authority.

| Tool | Behavior | Backend / receipt | Result | Permission |
|---|---|---|---|---|
| `attach_document` | Deferred | `/session/attachments.json` | `pending` | local |
| `create_agent` | API write | `POST /agents` | `applied` | `agents.write` |
| `archive_agent` | Deferred | `/session/archived_agents.json` | `pending` | local |
| `read_knowledge` | Local read | knowledge root | `ok` | local |
| `ask_clarification` | Deferred signal | `/session/clarification.json` | `awaiting_user` | local |
| `save_project_summary` | Deferred | `/session/saved_projects.json` | `pending` | local |
| `openviking_search` | Direct HTTP | OpenViking search API | `ok`/`failed` | local (config) |
| `openviking_read` | Direct HTTP | OpenViking content API | `ok`/`failed` | local (config) |
| `list_tasks` | API read | `GET /tasks` | `ok` | `tasks.read` |
| `get_task` | API read | `GET /tasks/{id}` | `ok` | `tasks.read` |
| `resolve_workspace` | API read | `GET /workspaces/resolve` | `ok` | `workspaces.read` |
| `create_task` | API write | preflight + `POST /tasks` | `applied` | `tasks.write` |
| `update_task` | API write | preflight + `PATCH /tasks/{id}` | `applied` | `tasks.write` |
| `get_task_logs` | API read | `GET /tasks/{id}` + format | `ok` | `tasks.read` |
| `resume_task_session` | API write | `GET` + `PATCH /tasks/{id}` | `applied` | `tasks.write` |
| `list_agents` | API read | `GET /agents` | `ok` | `agents.read` |
| `get_agent` | API read | `GET /agents/{id}` | `ok` | `agents.read` |
| `update_agent` | API write | `GET` + `PATCH /agents/{id}` | `applied` | `agents.write` |
| `list_skills` | Local read | `/skills` | `ok` | local |
| `install_skill` | Deferred | `/session/install_skills.json` | `pending` | local |
| `create_skill` | Deferred | `/session/created_skills.json` | `pending` | local |
| `list_knowledge` | Local read | knowledge root | `ok` | local |
| `write_knowledge` | Deferred | `/session/written_knowledge.json` | `pending` | local |
| `update_component_rules` | Deferred | `/session/written_knowledge.json` | `pending` | local |
| `trigger_review` | Operation | `POST /operations` kind `review` | `pending` | `review.trigger` |
| `trigger_commit_push` | Operation | `POST /operations` kind `git.commit_push` | `pending` | `git.actions` |
| `trigger_rollback` | Operation | `POST /operations` kind `git.rollback` | `pending` | `git.actions` |
| `trigger_analysis` | Operation | `POST /operations` kind `analysis` | `pending` | `analysis.trigger` |
| `list_analysis_reports` | API read | `GET /tasks/{id}/analysis` | `ok` | `analysis.read` |
| `get_analysis_report` | API read | `GET /analysis/{id}` | `ok` | `analysis.read` |
| `list_recommendations` | API read | `GET /recommendations` | `ok` | `analysis.read` |
| `preview_recommendation` | API read | `GET /recommendations/{id}` | `ok` | `analysis.read` |
| `approve_recommendation` | API write | `POST /recommendations/{id}/approve` | `applied` | `analysis.approve` |
| `apply_recommendation` | API write | `POST /recommendations/{id}/apply` | `applied` | `analysis.approve` |

The six analysis/recommendation tools are exposed to the secretary profile
only (`sec_tools` in `src/agent_manager/knowledge.py`); they are not added
unconditionally to every execution.

## Operation kinds and executors

Source: `src/agent_manager/operation_worker.py`,
`src/agent_manager/operation_handlers.py`.

| Kind | Registered executor | Effect |
|---|---|---|
| `git.commit_push` | `git_commit_push_handler` | `GitEngine.commit_and_push` / `commit_local`; marks task `done`. |
| `git.rollback` | `git_rollback_handler` | `GitEngine.rollback`; marks task `cancelled`. |
| `review` | `review_operation_handler` | `ReviewExecutor`, explicit verdict; never approves by absence. |
| `analysis` | `analysis_operation_handler` | `build_evidence_package` + analyzer runner; persists report/recommendations. |

Executors run outside any database transaction. Registration seam:
`OperationWorker.register(kind, handler)`; the built-in point is
`register_default_operation_handlers`.

## REST inventory

Source: `src/agent_manager/api/app.py`. Groups: health;
workspaces/resolve; tasks (create/read/list/patch/import, attachment upload via
`POST /tasks/{task_id}/attachments`, artifacts and per-task analysis);
conversations and messages; turns; operations (submit/list/read);
analysis reports; recommendations (list/preview/approve/apply); agents
(create/read/list/patch/archive/history). See
[Tools and API discovery](tools-and-api.md#rest-endpoints).

## Worker inventory

| Worker | Source | Claims | Effect |
|---|---|---|---|
| `QueueDispatcher` | `src/agent_manager/dispatcher.py` | pending turns | Runs secretary handler, stores reply. |
| `TaskExecutionWorker` | `src/agent_manager/task_worker.py` | `in_progress` tasks | Runs agent, `ReviewExecutor`, updates status. |
| `OperationWorker` | `src/agent_manager/operation_worker.py` | queued operations | Dispatches to registered executors. |

## Truthfulness rules

- A tool never reports `applied` or `pending` as a completed result.
- A deferred session write returns `pending` (or `awaiting_user` for
  clarification) and is acknowledged once; it is not replayed after being
  applied.
- An operation with no registered executor fails visibly with an
  unknown-executor diagnostic.
- OpenViking without per-instance configuration returns a specific
  "not configured" message.
- The analyzer has no apply path; approval and apply are separate calls and
  apply rechecks the target revision/hash.

## Parity checks

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_capabilities.py -q
```

`tests/policy_tool_names.mjs` enumerates the real registered tools; the test
compares them with the inventory and the live OpenAPI document. `tests/
platform_tools_contract.mjs` runs the real tools against a temporary HTTP
backend.

## Related

- `src/agent_manager/capabilities.py`
- `src/agent_manager/permissions.py`
- `tests/test_capabilities.py`, `tests/platform_tools_contract.mjs`
- `docs/plans/platform-completion/T05.md`
