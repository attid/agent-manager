# T08V — Integrate real-runtime fixes and final verification: result

Status: complete. Base committed HEAD when this task started:
`3d8db0db84d1d2751ffee893df8c21dceca7da35` (T07F verified AGY fixes).

Integrated source SHA (verified by the full suite and the real-model gate):
`af5ae71` (four T06R commits on top of `3d8db0d`). Final commit adds the
handbook reconciliation (`881793a`) and this report; neither touches `src/` or
`tests/`, so the verified source tree is byte-identical to `af5ae71`.

No nested agents, external reviews, pushes, deployments, production
API/Telegram/DB/KB access or credential reads were used.

## Integrated T06R commits

The immutable T06R commits (based on `c81c4b4`) were cherry-picked in order onto
`3d8db0d`, preserving their messages. Every one auto-merged textually; each
interaction point was then checked semantically (below).

| T06R | Integrated as | Summary |
|---|---|---|
| `415f128` | `4da2719` | parse real Runner output and review findings robustly |
| `a1ae67a` | `0471929` | align analyzer runtime defaults and profile applies with the contract |
| `1afc95f` | `ee0269a` | expand real-model acceptance to the Pirozhki lifecycle |
| `34abba0` | `af5ae71` | record T06R real-model acceptance result |

Later T06V/T07F changes are retained (revision-bound `/analyze`, task-scoped
permissions, `get_task_logs` structure, knowledge containment, native-session
fix resume).

## Contract combination checks

- **Session resume + Runner output extraction.** `TaskExecutionWorker` keeps
  both T07F's `session_id` parameter and T06R's `extract_runner_result`
  parsing. `_run_review_cycle` resumes the original developer session with
  `pi_runner resume --session <id>` plus the same state directory, role/runtime
  (`child_env`, `runner_argv_prefix`) and project workspace, and falls back to a
  fresh `run` only when no native session was reported. The step-level runner
  parse and the fix-round re-parse both use the shared helper.
- **Analysis containment + runtime resolver/string apply.** The T07F
  `contained_target_path` resolver is unchanged; T06R adds
  `runtime_defaults=getattr(instance, "runtime_defaults", None)` to
  `default_analyzer_runner` (the field already exists on
  `OperationInstanceContext`) and makes `_apply_profile` accept a non-empty
  prompt string and ignore echoed server-managed `agent_id`/`revision`.
- **Both workers' regression tests.** T06R `test_runner_output.py`,
  `test_task_worker_parses_pretty_printed_runner_result`,
  `test_execute_parses_pretty_printed_runner_result`, analyzer-resolver and
  apply-shape tests survive next to T07F's
  `test_review_changes_requested_resumes_original_developer_session` and
  knowledge-containment tests.

## Verification

All commands run with `PYTHONPATH=<worktree>/src` and `.venv/bin/python` on
Python 3.14.7. No output pipelines were used for the exit-code-bearing runs.

**Full suite** (source `af5ae71`):

```
PYTHONPATH=<worktree>/src .venv/bin/python -m pytest -o addopts='' -q --tb=short
# 463 passed, 2 skipped, 1 warning in 53.71s  (exit 0)
```

The two skips are the opt-in real-model tests without the env gate. The warning
is the existing Starlette/httpx deprecation.

**Relevant combined regressions before the full suite:**

```
PYTHONPATH=<worktree>/src .venv/bin/python -m pytest \
  tests/test_runner_output.py tests/test_review_executor.py \
  tests/test_task_worker.py tests/test_analysis.py -o addopts='' -q --tb=short
# 74 passed (exit 0)

PYTHONPATH=<worktree>/src .venv/bin/python -m pytest \
  tests/test_isolation_permissions.py tests/test_operation_permissions.py \
  tests/test_knowledge.py tests/test_review_operation.py \
  tests/test_telegram_bot.py tests/test_platform_tools_contract.py \
  tests/test_e2e_pirozhki.py -o addopts='' -q --tb=short
# 58 passed (exit 0)
```

**Automatic fix loop (native session/workspace/runtime), deterministic:**

```
PYTHONPATH=<worktree>/src .venv/bin/python -m pytest \
  "tests/test_task_worker.py::test_review_changes_requested_resumes_original_developer_session" \
  -o addopts='' -v --tb=short
# 1 passed in 1.71s (exit 0)
```

It drives the real `_run_review_cycle` over `subprocess` with the deterministic
child Runner, which enforces the same `resume --session` CLI contract, and
asserts the resume argv carries `--session <id>` and reuses the original state
directory/role. The full-suite run also covers fresh/upgraded databases
(`test_migrations.py`), scoped factory-issued permissions on populated state
(`test_isolation_permissions.py`), CLI parsing (`test_pi_runner_core.py`),
reviewer/analyzer default runtime (`test_review_executor.py`,
`test_analysis.py`) and proposal target safety (`test_knowledge.py`,
`test_analysis.py`).

**Real-model acceptance** (isolated harness, normal host authentication,
`bubblewrap`, no credential read; log `/tmp/t08v-evidence/real-final.log`):

```
AGENT_MANAGER_REAL_MODEL_E2E=1 PYTHONPATH=<worktree>/src \
  .venv/bin/python -m pytest tests/test_e2e_real_model.py -o addopts='' -q --tb=short
# 2 passed in 63.60s (exit 0)
```

**Static checks:**

```
node --check tests/e2e_pirozhki_tools.mjs
node --check tests/platform_tools_contract.mjs
node --check tests/policy_tool_names.mjs
node --check tests/policy_deferred_tools.mjs   # all exit 0
git diff --check 3d8db0d HEAD                  # exit 0
```

## Real acceptance accuracy and limitations

- The real gate really ran the production `pi_runner` through the isolated
  harness with the host-resolved provider/model and normal read-only auth
  resolution. Both tests passed.
- **The automatic multi-round review loop was disabled for the real lifecycle
  test** (`metadata["requires_review"] = False`) to keep the run bounded; the
  reviewer was exercised as one explicit durable `review` operation. T06R
  observed the automatic loop failing earlier in real runs 5–6; do not read the
  green real gate as a real automatic-loop approval.
- The combined implementation still supports the automatic loop with the
  original native session/workspace/runtime; that is covered deterministically
  by the fix-loop test above, not by the real gate.
- The real reviewer is a small nondeterministic model and may return
  `changes_requested`; the test accepts either domain verdict and does not
  weaken the finding. No automatic fix loop is run in the real gate.

## Interfaces changed by the integration

- `src/agent_manager/runner_output.py`: `extract_runner_result(stdout)`.
- `agent_manager.analysis.default_analyzer_runner` forwards
  `OperationInstanceContext.runtime_defaults`.
- `RecommendationService._apply_profile` accepts a non-empty prompt string and
  ignores echoed `agent_id`/`revision`.
- `tests/pirozhki_harness.py`: `build_pirozhki_instance`/`pirozhki_instance`
  accept `runtime_defaults`.
- `tests/test_e2e_real_model.py`: `test_real_model_secretary_conversation_turn`,
  `test_real_model_pirozhki_lifecycle`.

## Documentation reconciliation

- `docs/e2e-pirozhki-test-scenario.md` and `T06R-result.md` came with the T06R
  test commit and describe the two gates.
- `881793a` updates `docs/handbook/review.md` (fix round resumes the native
  session), `docs/handbook/analysis-and-recommendations.md` (stored report /
  `refresh` and the profile apply contract) and
  `docs/handbook/knowledge-and-integrations.md` (`cmd_analyze` behaviour).
- Earlier reports and the belief map were preserved; `.belief_map.sexp` was
  (re)built in this worktree and queried before source reads.

## Remaining gaps

- The opt-in real-model tests require an authenticated host Pi runtime; on a
  host without one they fail with the real runner diagnostic by design.
- The real automatic multi-round review loop remains covered only
  deterministically; the real gate intentionally runs a single bounded review.
- External final AGY review of the completed commits still follows this task.
- No production, deployment, push, Telegram or production data access was used.
