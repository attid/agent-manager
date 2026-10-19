# T02 — Instance isolation and execution permissions

- Base commit: `36661bc`
- Implementation commit: `238e075e04e2b50f7e308fd324bfb327926e4646`
- Branch: `platform/t02-isolation`
- Status: acceptance criteria implemented; bounded tests green.

## What changed

### 1. Tools are honored, never silently expanded

- `src/pi_runner/cli.py`: removed the unconditional append of every platform
  tool. `build_pi_command(config)` now passes exactly `config.tools` to
  `--tools`, so a restricted profile cannot acquire the platform surface
  through the launcher. Extracted `skill_arguments()` and `build_pi_command()`
  so the contract is testable without launching Pi.
- `src/agent_manager/knowledge.py`: developer profile lists its tools
  explicitly (`read`, `write`, `edit`, `bash`, `ask_clarification`,
  `read_knowledge`) instead of relying on the launcher append.
- `src/pi_runner/policy.mjs`: local/session tools (knowledge, skills,
  openviking, triggers, attach) are registered but only active when the
  profile requests them. No tool is auto-enabled.

### 2. Execution-scoped API actions and resources

- New `src/agent_manager/permissions.py` (reusable authorization seam):
  - `ExecutionPermissions` issues unpredictable in-memory tokens and holds
    `actions`/`resources`; `authorize()` / `resolve()` are the only checks.
  - `actions_for_tools()` derives backend actions strictly from configured
    tool names (no role or name allowlist).
  - `resources_for_execution()` scopes read-only executions to
    `task:<id>` / `agent:<id>`; management actions require the wildcard.
  - `resolve_requirement(method, path)` maps every HTTP route to its required
    action and resource.
- `src/agent_manager/api/app.py`: `BearerAuthMiddleware` now distinguishes
  administrative tokens (unrestricted) from execution tokens (enforced
  action + resource, `403` outside scope). The resolved grant is exposed at
  `scope["state"]["execution_grant"]`. `create_app(..., permissions=...)`
  accepts the shared registry and stores it on `app.state.permissions`.
- `src/agent_manager/task_worker.py` and
  `src/agent_manager/persistent_conversation.py` issue a scoped token per
  execution via the parent registry and place only that token in the child
  `role.json`/`backend_api.json`. Administrative tokens are never embedded.

### 3. Explicit per-instance configuration

- New `src/agent_manager/instance.py` (`InstanceContext`): the typed carrier
  for `state_root`, `workspaces_root`, `skills_root`, `knowledge_root`,
  `artifact_root`, default workspace and backend/OpenViking settings.
- `src/agent_manager/runner.py`: `RunnerConfig` gains `state_root`,
  `workspaces_root`, `skills_root`, `backend_url`, `openviking_url`,
  `openviking_token` and `instance_context()`. `AgentManagerServices` builds
  one context and passes it to the knowledge base, worker, conversation
  handler and API.
- `src/agent_manager/knowledge.py`: bootstrap uses the explicit
  state/workspace/default-workspace roots. A bare `KnowledgeBase(root_dir)`
  is instance-local (siblings of the knowledge tree), never host HOME.
- `src/agent_manager/persistent_conversation.py`: newly created agents and
  installed/created skills use the explicit instance roots.
- `src/agent_manager/telegram/worker.py`: the secretary media inbox uses the
  instance `workspaces_root` instead of the host HOME directory, and skips
  the copy with a warning when no instance knowledge base is attached.
- `src/agent_manager/api/app.py`: deleting an agent no longer constructs a
  host-HOME `KnowledgeBase()` when none is attached.
- `src/pi_runner/core.py`: `Config` gains explicit `knowledge_dir`,
  `skills_dir` and `backend`; `sandbox_command` mounts those exact paths to
  `/knowledge` and `/skills`.
- `src/pi_runner/cli.py`: `backend_api.json` is written only from the
  explicit role `backend` object. No env/admin/localhost/OpenViking fallback.
- `src/pi_runner/policy.mjs`: no compiled-in backend/OpenViking address or
  token. Unconfigured tools return a truthful "not configured" diagnostic.

## T01/T05 authorization seam

- The shared registry is `AgentManagerServices.permissions`
  (`ExecutionPermissions`) and is the same object passed to
  `create_app(..., permissions=...)`.
- Every new HTTP-backed tool/operation route must be added to
  `permissions._ROUTE_RULES` and/or checked with
  `ExecutionPermissions.authorize(token, action, resource)`. The middleware
  already enforces declared routes for execution tokens; an undeclared route
  is denied for execution tokens (fail-closed).
- Handlers can read the caller scope through
  `request.state.execution_grant` (set by the middleware) instead of
  re-parsing the token. T01 durable operations should re-issue a grant with
  the same action/resource when an interrupted execution is resumed.
- Management actions (`tasks.write`, `agents.write`, `knowledge.write`,
  `skills.write`, `git.actions`, `review.trigger`, `analysis.trigger`)
  require wildcard resources; read-only executions stay task/agent scoped.

## Validation

Command (run from the worktree with the supplied venv and `PYTHONPATH`):

```
PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t02-isolation/src \
  .venv/bin/python -m pytest \
  tests/test_api_auth.py tests/test_isolation_permissions.py \
  tests/test_pi_policy_tools.py tests/test_pi_runner_core.py \
  tests/test_runner.py tests/test_knowledge.py \
  tests/test_execution_contract.py tests/test_task_worker.py \
  tests/test_persistent_conversation.py tests/test_profile_runtime.py \
  tests/test_platform_tools_contract.py -q
```

Result: `91 passed` — `PYTEST_EXIT=0`.

Bounded regression batches also run green (exit 0):

```
tests/test_runner.py tests/test_task_worker.py tests/test_persistent_conversation.py \
tests/test_pi_reply.py tests/test_pi_failures.py tests/test_dispatcher.py \
tests/test_secretary.py tests/test_session_operations.py tests/test_api.py \
tests/test_agents.py tests/test_agents_api.py tests/test_platform_tools_contract.py

tests/test_conversation_runtime.py tests/test_conversations.py \
tests/test_conversations_api.py tests/test_api_resilience.py \
tests/test_runtime_defaults.py tests/test_repository.py \
tests/test_pi_credentials.py tests/test_logging.py

tests/test_telegram_bot.py tests/test_pi_policy_tools.py tests/test_imports.py \
tests/test_pi_runner_core.py
```

New evidence tests in `tests/test_isolation_permissions.py`:

- two independent temporary instances (separate DBs, roots, backend URLs and
  OpenViking addresses) produce independent files and developer profiles;
- a reviewer/analyzer credential built from `read`/`bash` cannot read another
  task, mutate agents, create tasks or resolve workspaces, even via raw HTTP;
- a `get_task` credential reads only its own task and is denied list/other
  task/agent access;
- a token from one instance registry is unknown to another;
- the task worker writes instance knowledge/skills mounts plus a scoped
  backend token and never the administrative token;
- `build_pi_command` exposes only the profile tools (`read,bash`).

`tests/test_pi_policy_tools.py` was updated because the previous expectation
(an implicit `http://127.0.0.1:18767` document and an OpenViking token with
no explicit configuration) is exactly the production fallback T02 removes.
It now asserts explicit instance backend values are written verbatim, that no
document is produced without explicit backend configuration, and that the
launcher adds no platform tool. The regression it still detects: a silent
localhost/administrative credential fallback or a launcher that widens the
active tool set.

## Remaining gaps

- The `/operations` endpoints and trigger dispatch from T01 are not present
  in this branch; `permissions._ROUTE_RULES` treats an undeclared route as
  denied for execution tokens, which T01/T05 must extend as they add routes.
- Grants are in-memory and live with the server process. Durable operation
  resume (T01/T05) must re-issue a grant from the persisted
  operator/execution identity.
- Telegram-only HOME defaults in `telegram/config.py` (state DB/media) and
  the legacy `create_bot(cfg)` fallback in `telegram/bot.py` are outside the
  T02 target list and remain. The unified `RunnerConfig` path always passes
  an explicit `InstanceContext` and knowledge base, so the running service
  does not use those fallbacks.
