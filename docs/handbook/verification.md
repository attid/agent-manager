# Verification

This handbook is documentation. Verification has two goals: (1) prove that
the described contracts match the code, and (2) give T05 a checklist for the
post-integration parity work.

## Bounded repository checks

Run from the repository root with the sandbox environment:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_operations.py \
  tests/test_platform_tools_contract.py \
  tests/test_api.py \
  tests/test_api_auth.py \
  tests/test_execution_contract.py \
  tests/test_persistent_conversation.py \
  -q
```

Interpretation:

- `test_operations.py` — durable operation protocol: submit/idempotency,
  claim fencing, complete/fail, orphan recovery.
- `test_platform_tools_contract.py` — executes the real `policy.mjs` tools
  against a temporary HTTP backend. It skips only when Node and the Pi
  TypeBox runtime are unavailable; a skip is not a pass.
- `test_api.py` / `test_api_auth.py` — REST contract and Bearer auth.
- `test_execution_contract.py` — workspace/execution context.
- `test_persistent_conversation.py` — session binding, deferred drains,
  operation submission from session actions.

Do not pipe test output in a way that hides the exit code unless
`set -o pipefail` is active. Report the command, outcome and exit code.

The final full suite and the end-to-end acceptance belong to the coordinator
(T05/T06); this handbook does not claim them.

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

A `pending`/`running` operation is not a result. A `review`/`analysis`
operation ending `failed` with `no executor registered` is expected in this
snapshot.

## T05 recheck checklist

- [ ] `llms.txt` and `docs/handbook/*` exist and link to each other.
- [ ] Every registered JS tool is represented in the capability inventory.
- [ ] Every advertised operation kind either has an executor or fails with a
      specific diagnostic.
- [ ] OpenAPI and the tool parameters/status codes agree.
- [ ] A short platform operating prompt is injected on every run/resume **and**
      customized identity prompts are preserved.
- [ ] Executable parity tests run the real tools, not stubs.
- [ ] Capabilities are filtered by actual permissions while full
      documentation stays discoverable.
- [ ] T02I scoped authorization is reintroduced without reopening anonymous
      access when a scope is revoked.
- [ ] Short/session writes are acknowledged and do not replay or revert newer
      state.
- [ ] T03 review and T04 analysis/recommendation interfaces are reconciled
      with this snapshot's placeholders.

## Related

- `docs/plans/platform-completion/T05.md`
- `tests/`
