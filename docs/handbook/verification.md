# Verification

This handbook is documentation. Verification has two goals: (1) prove that
the described contracts match the code, and (2) keep the integration parity
checks runnable. The executable inventory and parity check live in
`src/kuzya/capabilities.py` and `tests/test_capabilities.py`.

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
  tests/test_operational_api.py \
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
- `test_operational_api.py` — filtered operations listing, review-round
  artifact projection, conversation agent filter, metadata-only Runner
  session listing and verbose execution monitor.
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
  "from kuzya.api.app import create_app; \
   app=create_app('sqlite:////tmp/probe.db'); \
   [print(m.upper(), p) for p in sorted(app.openapi()['paths']) \
    for m in sorted(app.openapi()['paths'][p])]"

# Tool names:
grep -n 'name: "' src/pi_runner/policy.mjs

# Registered executors:
grep -n 'worker.register' src/kuzya/operation_handlers.py

# Hardcoded-findings gap (must be empty after T04):
grep -n 'Критического дефицита документации' src/kuzya/telegram/handlers.py
```

## Git state inspection (read-only)

To check what a Git operation or an agent actually changed, read the
repository instead of trusting a tool message or retrying the write. All of
these commands are read-only:

```bash
git status --porcelain=v1 -uall
git diff --cached --name-status
git diff --name-status
git show --stat --name-status HEAD
git log --oneline -5
git ls-tree -r --name-only HEAD | grep <expected-path>
git cat-file -e HEAD:<path>
git ls-files -- <path>
git stash list
```

How to read them:

- `git status --porcelain=v1 -uall` shows staged, unstaged and untracked
  entries, including files inside untracked directories. Empty output means
  the working tree matches the index and `HEAD`.
- `git diff --cached --name-status` is the staged set that the next commit
  would contain; `git diff --name-status` is the unstaged set.
- `git show --stat --name-status HEAD` is what is actually committed in
  `HEAD`; `git log --oneline -5` gives the recent commit chain.
- `git ls-tree -r --name-only HEAD | grep <expected-path>` and
  `git cat-file -e HEAD:<path>` prove that a path is present in the commit
  (a missing object is reported by the non-zero exit code).
- `git ls-files -- <path>` shows the index entry, and `git stash list`
  shows stashes that may hold uncommitted work.

In the sandbox, `.git` is commonly mounted read-only. That is expected:
read commands above work, while `git add`, `git commit` or anything that
creates `.git/index.lock` fails with `Read-only file system`. Do not treat
that error as a repository defect or try to repair `.git`; use the read
commands to establish state.

For the meaning of `metadata.changed_files` versus
`metadata.provenance.changed_files`, see
[Objects and links](objects.md#task).

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
- [x] OpenAPI, inventory, mounted adapter routes and tool schemas agree.
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
