# T07F — Verify and fix independent AGY findings: result

Status: complete. Base committed HEAD when this task started:
`ce8a5917967f775d5974f4f11a2d81821e6f7cbe`. Fixed implementation SHA:
`21848710b6842b3752a1f66b23c57ae2355cbd3d`.

Source of findings: `docs/drafts/agy_platform_pre_review.md` (review of
`fd62693..3af8653`). Every finding was reproduced against this worktree
before fixing; the AGY suggested remedies were treated as proposals only.
No nested agents, AGY, production access or deployment were used.

## Commits

```
2184871 fix: return structured get_task_logs result
2f782e1 fix: contain knowledge recommendation targets in instance subtree
4d0afac fix: show stored analysis report before launching a new one
30aaa47 fix: keep task-scoped execution tokens scoped
3078cf9 fix: resume developer review fix loop with native session id
```

The belief map was rebuilt in this worktree (`.belief_map.sexp`, 107 files)
and used to scope each boundary before reading source.

## Findings and disposition

### 1. Developer review fix loop resumed without a session id — CONFIRMED, FIXED

`TaskExecutionWorker._run_review_cycle` built
`pi_runner resume --state-dir ... --agent ... --task ...` with no
`--session`. The real CLI declares `--session` as required
(`src/pi_runner/cli.py:332`), so the child exited before the developer could
address any finding.

Reproduction (pre-fix):

```
PYTHONPATH=src .venv/bin/python -m pi_runner resume \
  --state-dir /tmp --agent /tmp/role.json --task /tmp/task.md
# usage: runner resume [-h] --session SESSION --state-dir STATE_DIR
# runner resume: error: the following arguments are required: --session
# exit code 2
```

The original developer run's `session_id` is now passed into
`_run_review_cycle`; the fix round resumes that exact session with the same
state directory, role/runtime and workspace, using the injected
`runner_argv_prefix`. If a runner reports no native session, the fix round
falls back to a fresh `run` instead of an invalid `resume`.

Regression test:
`tests/test_task_worker.py::test_review_changes_requested_resumes_original_developer_session`.
It drives the real `_run_review_cycle` over `subprocess` with the
deterministic child Runner (which enforces the same `resume --session`
contract), and asserts the resume argv carries `--session <id>`. Pre-fix it
fails with `RuntimeError: developer fix run failed with exit code 64`;
post-fix it passes.

### 2. Task-scoped execution token widened to wildcard — CONFIRMED, FIXED

`resources_for_execution` returned `{"*"}` whenever the action set touched
`MANAGEMENT_ACTIONS`, even with an explicit `task_id`. A developer whose
profile exposed `update_task`/`trigger_review` could then `PATCH` a foreign
task or submit a foreign operation over raw HTTP. The old
`test_scoped_grant_enforces_target_resource` masked this by passing the
resource set directly instead of using the real factory.

Reproduction: the three assertions below fail on the pre-fix permissions
module, e.g. `assert frozenset({'*'}) == frozenset({'agent:...', 'task:t1'})`.

`resources_for_execution` now always scopes a task-bound execution to
`task:<id>` plus `agent:<id>`. Only a conversation/turn-scoped execution with
no task identity (the secretary) keeps the wildcard its management tools
require.

Tests:

- `tests/test_isolation_permissions.py::test_task_scoped_management_tools_do_not_widen_to_wildcard`
  (task-scoped stays scoped; conversation-scoped secretary still wildcard).
- `tests/test_isolation_permissions.py::test_task_scoped_management_token_cannot_reach_foreign_task`
  (real middleware: own task writable, foreign task/collection/operation
  denied, own review accepted).
- `tests/test_operation_permissions.py::test_scoped_grant_enforces_target_resource`
  now uses the real factory output instead of a hand-written resource set.

### 3. `/analyze` always launched another model and never showed the report — CONFIRMED, FIXED

`cmd_analyze` called `client.trigger_analysis` unconditionally and never
listed stored reports, so a ready report was never displayed and each view
created another pending operation.

The handler now lists stored reports first and renders the newest one without
a model call. When no report exists it submits with an idempotency key bound
to the task's current revision (`updated_at`), so a repeated view while an
operation is pending replays the same durable operation. `/analyze <task_id>
refresh` submits a new operation explicitly, so an intentional fresh analysis
after changed evidence remains possible (a changed task revision also mints a
new key).

Tests:

- `tests/test_analysis.py::test_cmd_analyze_displays_stored_report_without_launching_analysis`
  (stored report rendered; `trigger_analysis` never called).
- `tests/test_analysis.py::test_cmd_analyze_refresh_forces_a_new_analysis`
  (refresh bypasses the stored report and sends no task-scoped key).
- `tests/test_analysis.py::test_cmd_analyze_replays_pending_and_reads_existing_report`
  (real handler + HTTP backend: two `/analyze` calls create one operation; a
  task edit creates a second; a stored report is read without a new
  operation).

### 4. Knowledge recommendation targets escaped the instance subtree — CONFIRMED, FIXED

`analysis._knowledge_target_path` joined `target_id` directly onto
`projects_dir`/`components_dir`, and the write methods did the same with no
containment check. A proposal with `target_id="../../escape"` wrote outside
the knowledge tree.

Reproduction (pre-fix, temporary manual probe):

```
knowledge = KnowledgeBase(root / "knowledge")
written = knowledge.create_or_update_project_summary("../../escaped", "PWNED")
# written: .../knowledge/projects/../../escaped/summary.md
# escaped_exists: True
```

A single resolver, `agent_manager.knowledge.contained_target_path`, now
resolves `base_dir/target_id/filename` and rejects absolute paths, `..`
traversal and symlinked escapes while still allowing broad nested names. It
is shared by recommendation preview, the observed evidence hash
(`_knowledge_target_path`) and the final write
(`create_or_update_project_summary`/`update_component_rules`). The analysis
executor refuses to persist a proposal whose target escapes.

AGY's suggested arbitrary `/`, `\`, `..` character allowlist was **rejected**:
the user forbids arbitrary product restrictions, and containment with nested
names is both broader and safer.

Tests:

- `tests/test_knowledge.py::test_contained_target_path_rejects_escapes_and_allows_nested_names`
  (nested name allowed and written; traversal, absolute path and symlink
  rejected; final write never creates the escaped file).
- `tests/test_analysis.py::test_analysis_rejects_recommendation_target_outside_knowledge_base`
  (analysis operation fails, no recommendation persisted, no file outside).

### 5. `get_task_logs` advertised `ok` but returned raw Markdown — CONFIRMED, FIXED

`capabilities.py` and the handbook advertise `get_task_logs` as an API read
returning `ok`/`failed`, but `policy.mjs` returned a Markdown string that no
JSON consumer could parse.

The tool now routes through the shared `backendRequest` helper and returns
`{status, task_id, title, task_status, updated_at, summary, error,
changed_files, review}`, with structured `failed` diagnostics
(`http_status`, `error`) on error. `docs/secretary-tools.md` was updated to
match.

Test: the registered JS tool is exercised in
`tests/platform_tools_contract.mjs` against the temporary HTTP backend
(`test_platform_tools_contract.py`), asserting the structured success shape,
empty `changed_files`, and the structured 404 failure. Pre-fix the contract
test fails with a JSON parse error.

## Verification

Per-fix regression runs (all exit code `0` after the fix):

```
PYTHONPATH=<worktree>/src .venv/bin/python -m pytest \
  tests/test_task_worker.py::test_review_changes_requested_resumes_original_developer_session -q -o addopts=''
# 1 passed

PYTHONPATH=<worktree>/src .venv/bin/python -m pytest \
  tests/test_isolation_permissions.py tests/test_operation_permissions.py tests/test_runner.py -q -o addopts=''
# 22 passed

PYTHONPATH=<worktree>/src .venv/bin/python -m pytest \
  tests/test_analysis.py tests/test_telegram_bot.py -q -o addopts=''
# 42 passed

PYTHONPATH=<worktree>/src .venv/bin/python -m pytest \
  tests/test_knowledge.py tests/test_analysis.py -q -o addopts=''
# 37 passed

PYTHONPATH=<worktree>/src .venv/bin/python -m pytest \
  tests/test_platform_tools_contract.py -q -o addopts=''
# 1 passed
```

Integrated relevant batch after all fixes (no output pipeline):

```
PYTHONPATH=<worktree>/src .venv/bin/python -m pytest \
  tests/test_task_worker.py tests/test_isolation_permissions.py \
  tests/test_operation_permissions.py tests/test_analysis.py \
  tests/test_knowledge.py tests/test_telegram_bot.py \
  tests/test_platform_tools_contract.py tests/test_runner.py \
  tests/test_review_executor.py tests/test_review_operation.py \
  tests/test_capabilities.py tests/test_pi_policy_tools.py \
  tests/test_e2e_pirozhki.py -q -o addopts=''
```

Outcome: `144 passed, 1 warning`, exit code `0` (warning is the existing
Starlette/httpx deprecation).

Static checks: `py_compile` on all changed Python files, `node --check` on
`policy.mjs` and the contract script, and `git diff --check` all exit `0`.

Each regression test was also run against the pre-fix source and observed to
fail, except finding 4, whose pre-fix escape was reproduced with the manual
probe above.

## Interfaces changed

- `agent_manager.knowledge.contained_target_path(base_dir, target_id, filename)`
  and `KnowledgePathError` are the shared containment resolver.
- `TaskExecutionWorker._run_review_cycle(..., session_id=None)` resumes the
  original developer session.
- `permissions.resources_for_execution` no longer widens a task-scoped grant;
  the wildcard requires `task_id is None`.
- Telegram `/analyze <task_id> [refresh]` reads stored reports and replays the
  same revision-bound operation.
- `get_task_logs` returns the structured `ok`/`failed` document.

## Remaining gaps

- The full suite was intentionally not re-run here: T07F defers it until the
  parallel T06R real-model work is integrated. The bounded integrated batch
  above covers every touched module plus the Pirozhki E2E.
- The opt-in real-model acceptance (`tests/test_e2e_real_model.py`) remains
  skipped and unverified in this sandbox, as recorded in `T06V-result.md`.
- Review reports, belief maps and worktrees were preserved; no production,
  deployment, push or credential access was used.
