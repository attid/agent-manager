# T02B — Stable authentication mode and internal client wiring

- Base commit: `74ed17e591c4a862a98f868d03d94b1ec5650f8b`
- Implementation commit: `c5add2390b6b8e063a2258fa210d693063f6e551`
- Branch/worktree: `platform/t02-isolation`
- Status: acceptance criteria implemented; bounded tests green (`126 passed`).

## Defect

`BearerAuthMiddleware` decided open vs protected mode on every request from
`not allowed_tokens and not permissions.enabled`, where `enabled` is the
current number of live execution grants. A formerly anonymous service
therefore flipped to protected when the first execution grant was issued
(breaking an unconfigured Telegram/backend client) and reopened anonymous
access when the last grant was revoked. Authorization must be a stable
per-instance decision.

## What changed

### 1. Stable, explicit auth mode

- `src/agent_manager/api/app.py`: `BearerAuthMiddleware` now accepts an
  explicit `require_auth` boolean that is fixed at construction. The
  per-request open-mode branch no longer reads `ExecutionPermissions.enabled`.
- `create_app` computes `require_auth = bool(admin_tokens) or
  permissions_provided` and exposes it as `app.state.require_auth`.
  Supplying an execution registry (the unified `AgentManagerServices` always
  does) protects the API from startup through grant revocation, before any
  scoped token exists and after the last is gone.
- Explicit open development mode now requires that the caller supply
  **neither** a registry **nor** administrative tokens. It is documented in
  the middleware and `create_app` docstrings and asserted in tests. A
  fresh `ExecutionPermissions()` is still created for standalone apps and
  exposed as `app.state.permissions`, but does not by itself protect them.

### 2. Host-only administrative principal for the Telegram client

- `src/agent_manager/runner.py`: `AgentManagerServices.resolve_host_auth()`
  builds the per-instance administrative token set and the Telegram client
  config. When a bot is configured it mints an ephemeral, in-memory
  `secrets.token_urlsafe(32)` internal token, registers it as an
  administrative credential and hands it to the real `BackendClient` built by
  `create_bot`. The token is never written to a file, child configuration or
  log; caller-supplied `api_tokens` remain valid alongside it.
- The service also aligns the Telegram `backend_url` with the instance
  `RunnerConfig.backend_url`, so the internal client talks to this process.
- `setup` stores the created `QueueProcessor` on `services.queue_processor`.

### 3. Grants are revoked when executions finish

- `src/agent_manager/task_worker.py`: the task-worker `step` keeps the issued
  `ExecutionGrant` and revokes its token in a `finally` once the child
  execution (including the review cycle) has finished.
- `src/agent_manager/persistent_conversation.py`: `_PreparedExecution` carries
  the issued `grant_token`, and `PersistentConversationHandler.__call__`
  revokes it in a `finally` after the turn's child call.
- Revocation removes only the scoped grant; it cannot change the stable
  `require_auth` decision or reopen anonymous access.

## Interfaces

- `create_app(..., permissions=...)` semantics extended: supplying a registry
  now protects the app from startup. `app.state.require_auth` is new and
  read-only evidence for callers/tests.
- `BearerAuthMiddleware(..., require_auth=None)` — explicit, optional keyword;
  defaults to the previous credential-source inference for direct
  construction.
- `AgentManagerServices.resolve_host_auth() -> (admin_tokens, telegram_config)`;
  `AgentManagerServices.internal_token: str | None`;
  `AgentManagerServices.queue_processor`.
- `ExecutionPermissions.issue`/`revoke`/`resolve`/`authorize` unchanged; the
  `task_worker`/`persistent_conversation` seam still receives the shared
  registry and children still receive only their scoped token.

## Validation

Command (run from the worktree with the supplied venv and `PYTHONPATH`):

```
PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t02-isolation/src \
  .venv/bin/python -m pytest \
  tests/test_api_auth.py tests/test_isolation_permissions.py \
  tests/test_runner.py tests/test_task_worker.py \
  tests/test_persistent_conversation.py tests/test_execution_contract.py \
  tests/test_platform_tools_contract.py tests/test_api.py \
  tests/test_telegram_bot.py tests/test_knowledge.py \
  tests/test_pi_policy_tools.py tests/test_pi_runner_core.py \
  tests/test_profile_runtime.py -o addopts=""
```

Result: `126 passed, 1 warning in 13.68s` — `PYTEST_EXIT=0`. No output
pipeline was used.

Mandatory-test coverage added/changed:

- `tests/test_api_auth.py::test_registry_auth_mode_stays_protected_across_grants`
  — (1) protected API denies anonymous mutation before any grant, and (2)
  still denies it after the last grant is issued and revoked.
- `tests/test_api_auth.py::test_admin_token_alone_protects_without_registry`
  — admin token alone is stable protection.
- `tests/test_api_auth.py::test_api_auth_open_mode_when_no_tokens` — explicit
  open development mode (`require_auth is False`) still allows anonymous
  mutation; documented deliberate choice.
- `tests/test_runner.py::test_service_created_telegram_client_uses_host_internal_principal`
  — runs the real `AgentManagerServices.setup`, the real `create_bot`
  `BackendClient` routed to the in-process ASGI app: the internal credential
  works before and during a secretary execution grant, and the scoped child
  credential is denied `tasks.read` collection, `tasks.write` and `agents.read`;
  after revocation the child token is rejected while the internal client and
  the closed anonymous mode are unchanged. The internal credential is only in
  the host process.
- `tests/test_persistent_conversation.py::test_conversation_execution_grant_is_scoped_and_revoked`
  — a secretary turn issues exactly one scoped `conversation:<id>` grant in the
  child role document and revokes it when the turn finishes.
- `tests/test_isolation_permissions.py::test_worker_issues_scoped_backend_and_never_admin_token`
  was updated: it now records issued grants (a recording registry) and asserts
  the same scope as before **plus** that the token is revoked after `step()`
  returns. The regression it still detects is a worker that issues an
  unscoped/unrevoked credential or embeds the administrative token.

## Remaining gaps

- The internal Telegram principal is an in-memory administrative token in the
  same `allowed_tokens` set as caller-supplied tokens; it is host-only by
  containment (never serialized to children or logs), not by source-address
  restriction.
- Grants remain in-memory and process-lifetime. Durable operation resume
  (T01/T05) must re-issue a grant from the persisted identity; this task does
  not touch T01 operation code.
- No update to `T02-result.md` was required: its base `36661bc` and
  implementation `238e075e04e2b50f7e308fd324bfb327926e4646` match the current
  `git log`; the original T02 evidence is preserved untouched.
