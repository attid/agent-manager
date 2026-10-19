# T06R — Real-model Pirozhki acceptance: result

Status: complete. Base `c81c4b4`. Final task commit `1afc95f`
(implementation and tests); this report is committed on top. The `src/`
tree exercised by the real run and the regression suite is byte-identical to
`1afc95f` — the real run predates only this report and one test-only
addition.

## Summary

The opt-in real-model gate was actually run against the production
`pi_runner` with normal host authentication (`opencode-go` /
`deepseek-v4.1-flash`) and the isolated Pirozhki harness. It exposed four
real defects, each fixed with a focused regression test, and the expanded
lifecycle gate now passes end to end.

Real-model acceptance result:

```
AGENT_MANAGER_REAL_MODEL_E2E=1 \
PYTHONPATH=src .venv/bin/python -m pytest tests/test_e2e_real_model.py \
  -q -o addopts='' --tb=short
# 2 passed in 71.95s (exit 0)
```

The run used no production Telegram, API, database or knowledge base, read
or persisted no credential, and pushed nothing.

## Commits

```
1afc95f test: expand real-model acceptance to the Pirozhki lifecycle
a1ae67a fix: align analyzer runtime defaults and profile applies with the contract
415f128 fix: parse real Runner output and review findings robustly
```

The untracked T06R task spec is committed with `1afc95f`.

## What landed

### 1. Real Runner JSON output is parsed (P1)

The real `pi_runner run` CLI prints one pretty-printed multi-line JSON
document (`json.dumps(result, indent=2)`). The task worker and the review
executor scanned for a *single* line starting and ending with a brace, found
none, and marked a successful developer execution as `Failed to parse runner
JSON output`. The deterministic fake Runner prints single-line JSON, so the
deterministic suite never saw this.

Fixed with a shared `src/agent_manager/runner_output.py`
(`extract_runner_result`) that parses the whole stripped document first and
falls back to the last balanced JSON object. Used from
`task_worker.step`, the developer-fix resume path, and
`review_executor._extract_last_json_object`.

Regressions: `tests/test_runner_output.py`,
`test_task_worker_parses_pretty_printed_runner_result`,
`test_execute_parses_pretty_printed_runner_result`.

### 2. Inline-code review findings are recognised (P2)

The reviewer model produced a valid `changes_requested` verdict with the P1
finding wrapped in backticks:
`` `P1: index.html:2 — ...` ``. `_FINDING_RE` required the severity at the
start of the line, so the finding was invisible, and the strict
"changes_requested must have a finding" rule failed the review. The regex
now accepts an optional list marker and surrounding backticks without
relaxing the severity/message requirement.

Regression: `test_parse_changes_requested_accepts_backtick_wrapped_finding`.

### 3. The analyzer follows the instance runtime selection (P1)

`default_analyzer_runner` built the frozen analyzer handler without passing
the instance `RuntimeDefaultsResolver`. The deterministic test hid this with
explicit `provider`/`model` overrides on the analyzer profile. Against a real
host the analyzer role had no provider/model and the child failed with
`Pi exited without a final assistant response` (`model: null`). The resolver
is now passed through.

Regression:
`test_default_analyzer_runner_uses_instance_runtime_defaults` asserts the
exported analyzer role carries the resolved provider/model/auth fields.

### 4. Profile recommendation apply matches the documented contract (P2)

The analyzer output contract permits "full proposed text" for
`proposed_content`, and real output used that form and echoed the observed
`agent_id`/`revision`. Apply rejected a string ("must be an object"), and
rejected the echoed server-managed fields as unsupported. Apply now treats a
non-empty string as the replacement `system_prompt` and ignores
`agent_id`/`revision` (the target is fixed by `target_id` and the revision is
re-checked from `target_revision`); genuinely unsupported fields still fail.

Regressions: `test_profile_recommendation_accepts_full_prompt_string`,
`test_profile_recommendation_ignores_server_managed_identity_fields`.

### 5. Expanded real-model lifecycle gate

`tests/test_e2e_real_model.py` now has:

- `test_real_model_secretary_conversation_turn` — readiness check (was the
  only test before).
- `test_real_model_pirozhki_lifecycle` — the real lifecycle:
  - the real secretary creates and routes the task through its platform
    tools (the test verifies the created/assigned task via `/tasks`, it does
    not fabricate it);
  - the real developer edits the temporary `pirozhki-shop` `index.html`
    through the production `TaskExecutionWorker`;
  - the reviewer runs as a durable `review` operation and returns a verdict;
  - the analyzer runs as the production `system-log-analyzer` profile through
    the durable `analysis` operation and produces an evidence report with
    recommendations;
  - the user explicitly approves and applies a recommendation through the
    normal API;
  - `git.commit_push(push=false)` makes a real local commit;
  - archive plus a knowledge-base re-initialization does not resurrect the
    profile.

`tests/pirozhki_harness.py` gained an injectable `runtime_defaults` resolver:
the deterministic tests keep an explicitly absent settings path so host Pi
settings can never influence them; the real acceptance resolves the host
provider/model and normal authentication. `docs/e2e-pirozhki-test-scenario.md`
describes both gates.

Explicit user decisions (start, approve, apply, commit, archive) are
simulated through the normal APIs. No real agent execution step is replaced
by a test script or a precomputed result.

## Defects found during the run (evidence)

- `lifecycle-run1.log`: developer completed with `changed_files:
  ["index.html"]` and `exit_code: 0`, but the task failed with "Failed to
  parse runner JSON output" — defect 1.
- `lifecycle-run2.log`: analyzer runner failed with
  `"model": null, "provider": null` — defect 3.
- `lifecycle-run5.log`: automatic review returned `changes_requested` with a
  backtick-wrapped P1, rejected as "no evidence-linked findings" — defect 2.
- `lifecycle-run7.log`: analysis succeeded, but profile apply rejected a
  string `proposed_content` and later echoed `agent_id`/`revision` — defect 4.
- `lifecycle-run8.log`: lifecycle passed after the fixes.
- `real-final.log`: both real tests passed.

Logs are preserved outside the repository under `/tmp/t06r-evidence/`
(scratch). They contain no credential values.

## Real-model execution details

- Runner: production `pi_runner` (`sys.executable -m pi_runner`) inside
  bubblewrap, with the real default system profiles
  (`seed_deterministic_configs=False`).
- Provider/model: resolved from the host Pi settings via
  `RuntimeDefaultsResolver`; the executed runner result reported
  `"provider": "opencode-go"`, `"model": "deepseek-v4.1-flash"`.
- Authentication: the runner's existing read-only credential resolution
  (`pi auth check --provider ... --credentials --no-refresh`); no credential
  value was printed, stored or committed.
- The acceptance disables the automatic multi-round review loop on the
  developer task (an explicit user review-policy decision recorded in task
  metadata) and runs the reviewer as one bounded durable review operation.
  The automatic review path and its real-run failure mode were still
  exercised in `lifecycle-run5/6.log`.
- In the final real run the reviewer returned `changes_requested` with a
  valid P1 (the `primary` class had no CSS rule, so the button was not
  actually green). The test accepts either domain verdict; it does not
  require approval and does not weaken the reviewer's finding.

## Validation commands and outcomes

Real-model acceptance (final):

```
AGENT_MANAGER_REAL_MODEL_E2E=1 PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_e2e_real_model.py -q -o addopts='' --tb=short
```

`2 passed in 71.95s`, exit `0`.

Affected regressions and deterministic E2E:

```
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runner_output.py tests/test_review_executor.py \
  tests/test_task_worker.py tests/test_review_operation.py \
  tests/test_analysis.py tests/test_e2e_pirozhki.py \
  -q -o addopts='' --tb=short
```

`81 passed in 11.87s`, exit `0`.

Static checks (all exit `0`):

```
.venv/bin/python -m py_compile <changed python files>
node --check tests/e2e_pirozhki_tools.mjs
git diff --check
```

## Interfaces and fixtures

- `src/agent_manager/runner_output.py`: `extract_runner_result(stdout)`.
- `tests/pirozhki_harness.py`: `build_pirozhki_instance` /
  `pirozhki_instance` accept `runtime_defaults`.
- `tests/test_e2e_real_model.py`: `test_real_model_secretary_conversation_turn`,
  `test_real_model_pirozhki_lifecycle`.
- `agent_manager.analysis.default_analyzer_runner` now forwards
  `OperationInstanceContext.runtime_defaults`.
- `RecommendationService._apply_profile` accepts a non-empty string
  `proposed_content` and ignores `agent_id`/`revision`.

## Remaining gaps

- The real-model tests are opt-in and depend on an authenticated host Pi
  runtime and network access; they are not part of the default suite. A host
  without a working provider will fail with the real runner diagnostic by
  design.
- The reviewer/analyzer are still small-model nondeterministic. The parser
  now accepts inline-code findings, but other malformed reports remain a
  visible failure rather than a silent approval, which is intentional.
- The lifecycle acceptance bounds the automatic multi-round review by
  disabling it for this task; the automatic loop is covered by the
  deterministic suite and was observed in real runs 5–6.
- T07 independent external review is still required by the plan.
- No production, deployment, push, Telegram/LLM credential read or
  production data access was used.
