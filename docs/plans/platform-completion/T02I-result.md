# T02I — Integrate operations with scoped execution permissions: result

Status: complete. Base `35c3982` (master with T01 `6aba3fd` and the T02I
acceptance commit). The four immutable T02 commits were cherry-picked
preserving their boundaries, then a final cross-task integration commit was
added.

## Integrated commits

Cherry-picked in order onto `35c3982`:

- `888fe05` feat: isolate instances and scope execution API permissions
  (from `238e075`)
- `273a8a0` docs: report T02 isolation and permission results (from `74ed17e`)
- `b80bd02` feat: make API auth mode stable and wire host internal principal
  (from `c5add23`)
- `79057e1` docs: report T02B stable auth mode and internal client wiring
  (from `df7a4ac`)

Conflicts were resolved by merging both implementations, never by taking a
whole file:

- `src/agent_manager/persistent_conversation.py`, `src/agent_manager/runner.py`:
  both the T01 operation imports and the T02 instance/permission imports.
- `tests/test_persistent_conversation.py`: both the T01 durable-operation test
  and the T02 scoped-grant test were kept as separate functions; the shared
  `finally: database.dispose()` was reconstructed once per test.

## Integration commit

`930aa2f` (this repository's final `feat:` commit) contains the
cross-task work:

### Operation instance seam

- `OperationInstanceContext` now carries `instance: InstanceContext` and
  `permissions: ExecutionPermissions`; `OperationContext` gains `owner`.
- `AgentManagerServices.setup` passes the shared instance context and
  permission registry into the operation worker, so a T03/T04 executor can
  re-issue a scoped backend grant while handling `review`/`analysis`.
- `OperationRepository.submit(..., owner=...)` persists the submitter
  identity; `owner` is `admin` or `execution:<agent>|<label>`, never a bearer
  token. Migration `0008_operation_owner` adds the nullable `owner` column;
  the head moves from `0007_operations` to `0008_operation_owner`.
- Session-array actions drained by `persistent_conversation` record the same
  execution owner as the conversation grant.

### `/operations` authorization

`resolve_requirement` marks `POST /operations`, `GET /operations` and
`GET /operations/{id}` as `deferred`: the middleware still authenticates the
token and exposes `request.state.execution_grant`, then the handler resolves
the concrete action and resource.

- `POST /operations`: `OPERATION_KIND_ACTIONS` maps `review` →
  `review.trigger`, `analysis` → `analysis.trigger`,
  `git.commit_push`/`git.rollback` → `git.actions`. A scoped caller must hold
  the kind's action and the `task:<target_id>` resource (wildcard for a
  targetless submit). A review grant can no longer post a Git action through
  the generic route; an unknown kind is admin-only.
- `ACTIONS_FOR_TOOLS` maps the real trigger tools
  (`trigger_review`, `trigger_analysis`, `trigger_commit_push`,
  `trigger_rollback`) to those actions, so profile tools drive the grant.
- `GET /operations`: a scoped caller is refused (`403`) — it cannot enumerate
  the queue.
- `GET /operations/{id}`: a scoped caller reads only an operation whose
  persisted `owner` matches its grant; a foreign or legacy ownerless record is
  reported `404` so existence is not disclosed. Administrative callers read
  every record.

### Trigger-tool idempotency

`src/pi_runner/policy.mjs` now derives the default idempotency key from
`[kind, target_id, input, toolCallId]`. Retrying the exact same invocation
reuses the operation; a later intentional invocation (`toolCallId` changed)
creates a new review/analysis after code changes even with identical input.
An explicit `idempotency_key` still overrides and is honored across
invocations.

## Interfaces handed to T03/T04

- `OperationInstanceContext(database, artifact_root, ..., instance,
  permissions)` — same `InstanceContext`/`ExecutionPermissions` objects as the
  rest of the runtime.
- `OperationContext.owner` — stable caller identity for orphan recovery.
- `OperationRepository.submit(..., owner=None)`; `Operation.owner`.
- Executors still register at `register_default_operation_handlers` /
  `OperationWorker.register`.

## Validation

Command (from the worktree, no output pipeline):

```
PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t02-integration/src \
  .venv/bin/python -m pytest \
  tests/test_operations.py tests/test_api_auth.py \
  tests/test_isolation_permissions.py tests/test_execution_contract.py \
  tests/test_task_worker.py tests/test_persistent_conversation.py \
  tests/test_runner.py tests/test_platform_tools_contract.py \
  tests/test_operation_permissions.py tests/test_migrations.py \
  tests/test_concurrency.py -q
```

Result: `96 passed`, exit code `0`.

Additional bounded run after the same code: `tests/test_pi_policy_tools.py
tests/test_imports.py tests/test_session_operations.py` — `17 passed`, exit
code `0`.

`tests/platform_tools_contract.mjs` runs the real `policy.mjs` tools against a
temporary HTTP backend and now asserts: same-`toolCallId` retry reuses the
operation id, a different `toolCallId` creates a distinct operation, and an
explicit key is honored across invocations.

`tests/test_operation_permissions.py` (new) populates unrelated operations and
exercises real raw HTTP with scoped tokens: review-vs-Git kind confusion,
per-target resource limits, unknown-kind denial, anonymous denial, queue
enumeration denial, foreign-operation `404`, owner persistence, and that an
administrative token never appears in the serialized operation JSON.

## Changed existing tests and why

- `tests/test_migrations.py`: the single head assertion moved from
  `0007_operations` to `0008_operation_owner` because the new migration adds
  the `owner` column; the test still asserts exactly one current head row. A
  new assertion checks the `operations.owner` column exists after a fresh
  migrate.
- `tests/platform_tools_contract.mjs`: `call(...)` now takes an invocation id
  and the trigger section added the same-invocation/different-invocation and
  explicit-key assertions described above. Existing assertions are unchanged.

## Remaining gaps

- The belief-map layer heuristic classifies `permissions.py` as the `api`
  layer, so `operation_worker → permissions` (and the pre-existing
  `task_worker`/`persistent_conversation` imports) appear as boundary
  "violations". This is a heuristic classification of a shared authorization
  module, not a new runtime dependency direction; the import is required by
  the requested seam.
- `owner` is intentionally not exposed in `OperationRead`; T03/T04 read it
  from `OperationContext`/the repository.
- T03 review and T04 analysis executors are still unregistered and fail
  visibly; this task adds the permission/ownership seam only.
- The Telegram internal principal remains an in-memory administrative token
  (from T02B); no source-address restriction was added.
