# Verification

This handbook is documentation. Verification has two goals: (1) prove that
the described contracts match the code, and (2) keep the integration parity
checks runnable. The executable inventory and parity check live in
`src/agent_manager/capabilities.py` and `tests/test_capabilities.py`.

## Bounded repository checks

Run from the repository root with the sandbox environment:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_capabilities.py \
  tests/test_operations.py \
  tests/test_operation_permissions.py \
  tests/test_isolation_permissions.py \
  tests/test_platform_tools_contract.py \
  tests/test_review_executor.py \
  tests/test_review_operation.py \
  tests/test_analysis.py \
  tests/test_session_trace.py \
  tests/test_knowledge.py \
  tests/test_api.py \
  tests/test_api_auth.py \
  tests/test_execution_contract.py \
  tests/test_persistent_conversation.py \
  tests/test_migrations.py \
  -q
```

Interpretation:

- `test_capabilities.py` — inventory ↔ OpenAPI ↔ registered-tool parity,
  permission-mapping consistency and discovery filtering.
- `test_operations.py` — durable operation protocol: submit/idempotency,
  claim fencing, complete/fail, orphan recovery.
- `test_operation_permissions.py` / `test_isolation_permissions.py` — kind/
  target-scoped `/operations` authorization, owner-scoped reads, raw-HTTP
  analysis/recommendation authorization and instance isolation.
- `test_platform_tools_contract.py` — executes the real `policy.mjs` tools
  against a temporary HTTP backend. It skips only when Node and the Pi
  TypeBox runtime are unavailable; a skip is not a pass.
- `test_review_executor.py` / `test_review_operation.py` — explicit review
  verdicts, failure diagnostics and the durable review handler.
- `test_analysis.py` — evidence package, report/recommendation persistence,
  approval and stale-apply conflict.
- `test_session_trace.py` — target-task session/event trace, missing artifacts,
  path isolation, secret redaction and content-argument omission.
- `test_api.py` / `test_api_auth.py` — REST contract and stable Bearer auth.
- `test_knowledge.py` — knowledge writes, containment and exact-file deletion
  (traversal, symlinks, `agents/`, wildcards and git commits).
- `test_execution_contract.py` — workspace/execution context.
- `test_persistent_conversation.py` — session binding, deferred drains,
  operation submission from session actions and discovery injection.
- `test_migrations.py` — the linear chain to head `0009_analysis`.

Do not pipe test output in a way that hides the exit code unless
`set -o pipefail` is active. Report the command, outcome and exit code.

The final full suite and the end-to-end acceptance belong to the coordinator
(T06); this handbook does not claim them.

## Static discovery checks

```bash
# Endpoints (no database opened at import):
PYTHONPATH=src .venv/bin/python -c \
  "from agent_manager.api.app import create_app; \
   app=create_app('sqlite:////tmp/probe.db'); \
   [print(m.upper(), p) for p in sorted(app.openapi()['paths']) \
    for m in sorted(app.openapi()['paths'][p])]"

# Tool names:
grep -n 'name: "' src/pi_runner/policy.mjs

# Registered executors:
grep -n 'worker.register' src/agent_manager/operation_handlers.py

# Hardcoded-findings gap (must be empty after T04):
grep -n 'Критического дефицита документации' src/agent_manager/telegram/handlers.py
```

## Runtime checks (isolated instance only)

Never use production endpoints, databases or Telegram.

```bash
# Submit an operation and read it back by the same id (idempotent retry):
curl -s -X POST http://127.0.0.1:18767/operations \
  -H 'Content-Type: application/json' \
  -d '{"kind":"git.rollback","target_id":"<task-id>","idempotency_key":"demo-1"}'
curl -s http://127.0.0.1:18767/operations/<operation-id>
```

A `pending`/`running` operation is not a result. Inspect `status`, `result`
and `error`; a `review` or `analysis` operation is `succeeded` only after its
registered executor produced a validated result.

## Completion checklist

- [x] `llms.txt` and `docs/handbook/*` exist and link to each other.
- [x] Every registered JS tool is represented in the capability inventory and
      checked by `tests/test_capabilities.py`.
- [x] Every advertised operation kind has a registered executor.
- [x] OpenAPI, inventory and tool schemas agree.
- [x] A short platform operating prompt is injected on every run/resume and
      customized identity prompts are preserved.
- [x] Parity tests enumerate the real registered tools (Node) and the live
      OpenAPI document.
- [x] Capabilities are filtered by actual permissions while full
      documentation stays discoverable.
- [x] Scoped authorization is stable across grant revocation and never
      reopens anonymous access.
- [x] Session-deferred writes return a structured status and are acknowledged
      once.
- [x] T01–T05 interfaces are integrated on one linear migration chain
      (`0009_analysis`).

Future corrections belong in code and the inventory first; update this
handbook in the same change.

## Related

- `docs/plans/platform-completion/T05.md`
- `tests/`
