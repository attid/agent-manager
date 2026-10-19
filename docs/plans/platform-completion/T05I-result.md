# T05I — Delegated integration and complete capabilities: result

Status: complete for the delegated integration scope. Base `fc7a3f7`;
implementation HEAD `18e9ea804d21c41816eb79697ef7ac66f8997c45` on
`platform/t05-integration` (this report is the branch tip on top of it).

All integration inputs were supplied in `T05I-handoff.md` and integrated:
T02I (`35c3982..cfe3cd3`), T03 (`f62c663`, `ddb3536`), T04 (`89424db`,
`330c033`) and T05D (`3ec4847`). No moving branch was copied; each task's
immutable commits were cherry-picked and conflicts were resolved by merging
contracts, never by taking a whole file.

## Commits (this task)

```
811935c feat: add capability inventory, parity checks and handbook discovery
bdc3719 feat: add independent review executor and operation handler   (T03)
d3f144e docs: record T03 independent review result and interfaces      (T03)
b8537ed feat: add evidence-backed analysis, recommendations and approvals (T04)
5741225 docs: record T04 analysis and recommendation result           (T04)
90202aa docs: add platform agent handbook and llms.txt                (T05D)
108e750 fix: integrate review and analysis executors with discovery and scopes
543ea0c docs: reconcile handbook with the integrated T02I-T05 behaviour
18e9ea8 docs: record T05I integration notes on the task reports
```

Earlier in the same branch: T02I cherry-picks `6984cad`, `29927f2`,
`b3881f1`, `8062b65`, `5c0f249`, `2629e26`.

## What landed

### Machine-readable inventory and parity

- `src/agent_manager/capabilities.py` declares all 34 registered JS tools and
  31 REST endpoints with `method`, `path`, `request_schema`,
  `response_schema`, result statuses, `action`, `operation_kind` and handbook
  `doc`. `inventory_document()` is the JSON-compatible form.
- `audit_capabilities(openapi_paths=..., registered_tool_names=...)` compares
  the declaration with the live FastAPI OpenAPI document, the tools actually
  registered by `policy.mjs` and `ACTIONS_FOR_TOOLS`. It reports stable codes
  (`undeclared-tool`, `missing-tool`, `unsupported-route`,
  `undocumented-route`, `request-schema-drift`, `response-schema-drift`,
  `route-action-drift`, `tool-action-drift`, `widening-tool-action`,
  `operation-kind-action-drift`) and raises on any finding.
- `tests/test_capabilities.py` runs the audit using
  `tests/policy_tool_names.mjs` to enumerate the real registered tools (Node +
  Pi TypeBox; the OpenAPI checks still run when Node is absent). A negative
  test proves the checker is not vacuous.

### Discovery injection

- `render_handbook_discovery(tools)` always lists the stable handbook index
  (`docs/handbook/*.md`, also copied to `<knowledge_root>/handbook` and
  mounted at `/knowledge/handbook`) but lists only the profile's enabled
  platform tools (`executable_tool_names`).
- `is_platform_agent` selects the secretary (has `create_task`) and every
  `system-*` agent. `write_handbook_discovery` writes the extra instruction
  file for task executions and executors.
- Injection is appended as an extra instruction document, never to the stored
  `system_prompt`: `PersistentConversationHandler._prepare_execution`
  (`knowledge_catalog.md`), `task_worker` (`platform_discovery.md`),
  `ReviewExecutor` (reviewer role) and `freeze_profile_handler`
  (`extra_instructions`, used by `default_analyzer_runner` for the analyzer).
- `KnowledgeBase.sync_handbook` copies `docs/handbook/*.md` into the knowledge
  root content-idempotently.

### Advertised tool/API gaps closed

- `archive_agent`, `save_project_summary`, `install_skill`, `create_skill`,
  `update_component_rules` now return structured `{status: "pending", ...}`
  results; `ask_clarification` returns `{status: "awaiting_user", ...}`;
  `resume_task_session` uses `backendRequest` and returns
  `{status: "applied", ...}`. No deferred write reports success as text only.
- `archive_agent` no longer grants `agents.write`: it applies in-process from a
  session receipt, so mapping it to an HTTP action silently widened a
  profile's raw-HTTP authority. `actions_for_tools(["archive_agent"])` is now
  empty.
- OpenViking tools keep the explicit per-instance configuration gate.

### Integration of T03/T04 with T02I

- `OperationInstanceContext` carries `instance`, `permissions`,
  `runtime_defaults` and `analyzer_runner` together;
  `register_default_operation_handlers` registers git, review and analysis.
- The analysis/recommendation routes are mapped in `permissions.py`:
  `analysis.read` for report/recommendation reads and the management action
  `analysis.approve` for approve/apply. A task-scoped `analysis.read` grant
  can read its own `GET /tasks/{id}/analysis` but not another report by id;
  `analysis.trigger` cannot approve; approve/apply cannot submit Git. This is
  enforced through the same middleware for raw HTTP and tools
  (`tests/test_operation_permissions.py`).
- The six analysis/recommendation tools are exposed through the secretary
  profile (`sec_tools`) only; `cli.py` keeps the T02I explicit-runner
  behaviour and does not restore unconditional platform tools.
- Migrations are one linear chain: `0007_operations` → `0008_operation_owner`
  → `0009_analysis`. The T04 migration file is `migrations/versions/
  0009_analysis.py` with `down_revision = 0008_operation_owner`; fresh migrate
  and repeated upgrade both pass.

## Verification

Command (from the worktree, `PYTHONPATH=src`, no output pipeline):

```
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_capabilities.py tests/test_operations.py \
  tests/test_operation_permissions.py tests/test_isolation_permissions.py \
  tests/test_platform_tools_contract.py tests/test_review_executor.py \
  tests/test_review_operation.py tests/test_analysis.py tests/test_api.py \
  tests/test_api_auth.py tests/test_execution_contract.py \
  tests/test_persistent_conversation.py tests/test_migrations.py \
  tests/test_task_worker.py tests/test_profile_runtime.py \
  tests/test_concurrency.py tests/test_conversation_runtime.py \
  tests/test_telegram_bot.py tests/test_imports.py \
  -o addopts="" -q -p no:cacheprovider
```

Outcome: `226 passed`, exit code `0`.

`tests/policy_tool_names.mjs` and `tests/policy_deferred_tools.mjs` were run
by `test_capabilities.py`; `tests/platform_tools_contract.mjs` executes the
real `policy.mjs` tools against a temporary HTTP backend (including the new
structured `resume_task_session` result). The belief map was rebuilt; the new
`capabilities`, `analysis` and `review_executor` modules add no boundary
finding. `boundaries all` reports 11 heuristic layer findings, all on shared
authorization/runtime modules (`permissions`, `runtime_defaults`) classified
as `api`, consistent with the pre-existing T02I note.

## Public interfaces added or confirmed

- `agent_manager.capabilities`: `Capability`, `CAPABILITIES`,
  `inventory_document`, `audit_capabilities`, `Finding`, `CapabilityAudit`,
  `render_handbook_discovery`, `executable_tool_names`, `is_platform_agent`,
  `write_handbook_discovery`, `HANDBOOK`.
- `agent_manager.permissions`: `ANALYSIS_READ`, `ANALYSIS_APPROVE`;
  `ACTIONS_FOR_TOOLS` entries for the six analysis tools; route rules for the
  seven analysis/recommendation routes.
- `profile_runtime.freeze_profile_handler(..., extra_instructions=...)`.
- `KnowledgeBase.sync_handbook(source=None)`.
- Health of the T03/T04 seams: `review_operation_handler`,
  `analysis_operation_handler`, `OperationInstanceContext.analyzer_runner`.

## Remaining gaps and handoff

- T06 (isolated deterministic Pirozhki E2E and opt-in real-model acceptance)
  and the final full suite are the coordinator's; this task ran a bounded set.
- T07 independent external AGY review is still required before reporting
  platform completion. Do not claim completion without it.
- The default analyzer runner (`default_analyzer_runner`) is not exercised by
  a real model here; it is covered through the deterministic injected runner
  path plus the new `extra_instructions` unit test.
- `docs/plans/platform-completion/T05D-result.md` is a historical snapshot;
  it now carries an integration note, and `docs/handbook/*` and `llms.txt` were
  reconciled in place.
- No production, deployment, push or real Telegram/LLM access was used.
