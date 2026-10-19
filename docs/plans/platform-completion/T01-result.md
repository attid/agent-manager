# T01 — Durable operations and truthful tools: result

Status: complete. Implementation commit `1c6f433` on `platform/t01-operations`
(base `36661bc`).

## What landed

A durable `Operation` queue between deferred platform triggers and their
executors, plus an HTTP contract that makes submission, status, result and
error inspectable.

- ORM `Operation` (`src/agent_manager/storage/models.py`) and additive Alembic
  migration `migrations/versions/0007_operations.py`.
- `OperationRepository` (`src/agent_manager/storage/operations.py`).
- `OperationWorker` + registration seam
  (`src/agent_manager/operation_worker.py`).
- Built-in Git executors and default registration
  (`src/agent_manager/operation_handlers.py`).
- `POST /operations`, `GET /operations`, `GET /operations/{id}`
  (`src/agent_manager/api/app.py`, `schemas.py`).
- Trigger tools submit operations and return the operation
  (`src/pi_runner/policy.mjs`).
- Legacy `actions.json` processed through idempotent operation submission;
  session arrays are acknowledged once (`persistent_conversation.py`).
- Runner composition root creates the worker, registers Git executors and
  starts/stops it (`runner.py`).

## Public interfaces for T03/T04

Operation record fields: `id`, `kind`, `target_id`, `input` (JSON),
`status`, `result` (JSON|null), `error`, `idempotency_key`, `claim_token`,
`worker_id`, `attempt_count`, `created_at`, `updated_at`, `claimed_at`,
`finished_at`, `metadata`.

Protocol statuses (`agent_manager.storage`): `pending`/`accepted` queued,
`running` claimed, `succeeded`/`failed`/`interrupted` terminal. A domain
verdict (e.g. `approved`/`changes_requested`) belongs inside `result`, never
in `status`.

Repository:
`OperationRepository.submit(*, kind, target_id=None, input=None,
idempotency_key=None, metadata=None) -> OperationSubmitResult`;
`get(id)`, `list(*, status, kind, target_id, limit, offset)`,
`claim_next(*, worker_id=None)`, `complete(id, claim_token, *, result=None)`,
`fail(id, claim_token, *, error)`, `recover_orphans(*, reason)`,
`requeue_interrupted(id)`. Errors: `OperationConflictError`,
`OperationNotFoundError`, `OperationStaleClaimError`.

Worker registration seam:

```python
worker.register("review", handler)
def handler(context: OperationContext) -> Mapping[str, Any] | None: ...
```

`OperationContext` carries `operation_id`, `kind`, `target_id`, `input`,
`attempt_count`, `metadata` and `instance` (`OperationInstanceContext`:
`database`, `artifact_root`, `runner_argv_prefix`, `default_workspace`,
`knowledge_base`, `workspace_map`). Registering the same kind again replaces
the handler, so T03/T04 can attach real review/analysis executors at
`register_default_operation_handlers` without touching the queue.

Kind constants: `OPERATION_KIND_REVIEW="review"`,
`OPERATION_KIND_ANALYSIS="analysis"`,
`OPERATION_KIND_GIT_COMMIT_PUSH="git.commit_push"`,
`OPERATION_KIND_GIT_ROLLBACK="git.rollback"`.

HTTP: `POST /operations` returns `202` on insert, `200` on idempotent replay,
`409` on a reused key with a changed payload; `GET /operations/{id}` returns
the record or `404`.

## Validation

```
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_operations.py tests/test_persistent_conversation.py \
  tests/test_migrations.py tests/test_concurrency.py \
  tests/test_conversation_runtime.py tests/test_platform_tools_contract.py \
  tests/test_execution_contract.py tests/test_api.py tests/test_agents_api.py \
  tests/test_conversations_api.py tests/test_reconciliation.py \
  tests/test_runner.py -q
```
Outcome: `126 passed`, exit code `0`.

Earlier bounded runs, all exit code `0`:
`tests/test_operations.py tests/test_migrations.py
tests/test_session_operations.py tests/test_platform_tools_contract.py`;
`tests/test_persistent_conversation.py tests/test_runner.py`;
`tests/test_api.py tests/test_api_auth.py tests/test_execution_contract.py
tests/test_reconciliation.py tests/test_conversations_api.py
tests/test_agents_api.py`;
`tests/test_task_worker.py tests/test_dispatcher.py tests/test_imports.py
tests/test_concurrency.py tests/test_pi_policy_tools.py
tests/test_api_resilience.py`;
`tests/test_repository.py tests/test_agents.py tests/test_secretary.py
tests/test_conversations.py tests/test_knowledge.py tests/test_git_engine.py
tests/test_runtime_defaults.py tests/test_profile_runtime.py
tests/test_logging.py`;
`tests/test_pi_reply.py tests/test_pi_runner_core.py tests/test_pi_failures.py
tests/test_pi_credentials.py tests/test_pi_integration.py`.

`tests/test_platform_tools_contract.py` executes the real `policy.mjs` tools
against a temporary HTTP backend and asserts that `trigger_review`/
`trigger_analysis` return pending operations and that a same-input retry
returns the same operation id.

The belief map was rebuilt after the structural change; the new modules
appear in the graph and add no architecture-boundary violation.

## Changed existing tests and why

- `tests/test_migrations.py`, `tests/test_conversation_runtime.py`: the head
  revision moved from `0006_task_agent` to `0007_operations`; the assertions
  still verify a single head row and full migration completion.
- `tests/test_concurrency.py`: the model/migration drift check gained the new
  `operations` table, still asserting metadata exactly equals the migrated
  schema.
- `tests/test_persistent_conversation.py`: import and a new test for action
  routing; existing assertions unchanged.

## Remaining gaps / handoff

- Review (`review`) and analysis (`analysis`) executors are intentionally not
  registered; their operations fail visibly with
  `OperationExecutionError: no executor registered ...`. T03/T04 register
  them via `OperationWorker.register`.
- Git executors are registered in the runner composition root only. Git push
  is never exercised by tests; unit tests use fake handlers.
- README/handbook documentation of `/operations` is deferred to T05, which
  owns the discoverable platform contract. Interfaces are listed above.
- The legacy `actions.json` path remains only to drain old session artifacts;
  new tools submit directly. Session action payloads accept the existing
  `trigger_*` shape.
- `OperationWorker.start()` reconciles orphaned `running` operations at
  startup. There is no automatic retry; `requeue_interrupted` is explicit.
