# T06V — Delegated isolated acceptance and integrated verification: result

Status: complete for the delegated scope. Base `3af8653` (current committed
HEAD when the task started). Verified source SHA: `180f525` (the tree the
full suite below ran against; this report only adds documentation on top).
Task spec `T06V.md` is committed alongside this report.

## Commits (this task)

```
180f525 test: assert strict review failure in Pirozhki acceptance
9a8bf37 test: add isolated deterministic Pirozhki end-to-end acceptance
dd515f1 fix: persist applied profile recommendation to knowledge base
```

`dd515f1` is the source fix and its focused regression; `9a8bf37` adds the
deterministic E2E, the real-model gate and the updated scenario; `180f525`
adds the strict-failure acceptance. This report and the task spec are
committed on top.

## What landed

### Deterministic isolated Pirozhki E2E

`tests/pirozhki_harness.py` composes the real production components against a
disposable instance:

- migrated temporary file SQLite (real Alembic chain);
- temporary Git-backed knowledge base and `pirozhki-shop` project repository;
- temporary instance state/artifacts;
- real `QueueDispatcher` + `PersistentConversationHandler` (secretary),
  `TaskExecutionWorker` (developer) and `OperationWorker` with
  `register_default_operation_handlers` (review, analysis, Git);
- real uvicorn HTTP backend on an ephemeral loopback port;
- deterministic child executions via `tests/fake_pi_runner.py`;
- the real `src/pi_runner/policy.mjs` tools executed by Node.

`tests/e2e_pirozhki_tools.mjs` drives `create_agent`, `create_task`,
`update_task`, `trigger_review`, `trigger_analysis` and the recommendation
tools against the same backend and prints JSON for the Python assertions.

`tests/test_e2e_pirozhki.py` covers, in one lifecycle plus two focused tests:

- a real conversation turn through the dispatcher and the deterministic
  Runner, with the completed turn read back over HTTP;
- a review that returns no explicit verdict fails the operation and never
  approves the task by absence of findings;
- JS-tool creation of `pirozhki-developer` and the task, then acceptance and
  execution in the exact project workspace (`execution_context.host_path` /
  `sandbox_path`);
- the developer child writing a real working-tree change, automatic review,
  and a standalone review of the completed task without rerunning the
  developer (real report and verdict on disk);
- production `default_analyzer_runner` (`system-log-analyzer`) producing a
  report with bounded evidence and evidence-linked recommendations;
- profile recommendation preview/approve/apply through the JS tools,
  advancing the profile revision;
- project recommendation apply updating `projects/pirozhki-shop/summary.md`
  and committing to the knowledge Git repository;
- a stale recommendation conflict (`409`, persisted `conflict`) that never
  overwrites newer content;
- `git.commit_push(push=false)` producing a real local commit and `done`;
- archival (soft-delete plus knowledge-file removal and git commit) and no
  resurrection after a restart-style `KnowledgeBase.initialize`;
- restart preservation of conversations, messages, tasks and operations,
  idempotent `POST /operations` replay across restart, and orphan
  reconciliation to `interrupted`;
- negative scoped API permissions on populated state (own task readable,
  foreign task and collection denied, write denied, review grant cannot
  submit Git, anonymous request `401`).

Acceptance assertions use persisted worker output; no test inserts an
asserted result directly into the database.

### Confirmed defect and fix

The E2E exposed that `RecommendationService.apply` for a `profile` target
updated the database but not the Git-backed `agents/<id>.md` export. A
restart-style `sync_agent_from_file` then reverted the approved prompt.
`src/agent_manager/analysis.py` now writes the applied profile through
`KnowledgeBase.save_agent_profile` when a knowledge base is configured.
`tests/test_analysis.py::test_applied_profile_recommendation_survives_knowledge_file_sync`
regression-fails without the fix and passes with it.

### Opt-in real-model acceptance

`tests/test_e2e_real_model.py` is skipped by default and runs only when
`AGENT_MANAGER_REAL_MODEL_E2E` is truthy. It builds the same isolated instance
with the production `pi_runner` prefix and the real default system profiles
(no deterministic overrides), drives one secretary turn, and reports the
actual runner diagnostic on failure. It never reads or persists credentials
and never touches production Telegram/API/data.

## Verification

Deterministic acceptance plus the focused regression:

```
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_e2e_pirozhki.py tests/test_e2e_real_model.py \
  tests/test_analysis.py::test_applied_profile_recommendation_survives_knowledge_file_sync \
  -q -o addopts='' --tb=short
```

Outcome: `5 passed, 1 skipped, 1 warning`, exit code `0`. The skip is the
opt-in real-model test.

Full suite (as required by T06V, no output pipeline):

```
PYTHONPATH=src .venv/bin/python -m pytest -o addopts='' -q --tb=short
```

Outcome: `447 passed, 1 skipped, 1 warning`, exit code `0`. The skip is the
opt-in real-model test.

Static checks:

```
node --check tests/e2e_pirozhki_tools.mjs   # exit 0
.venv/bin/python -m py_compile <changed python files>   # exit 0
git diff --check   # exit 0
```

### Real-model status

Not run. The gate is present and skipped by default; no safe isolated
real-model invocation was attempted. Enabling it requires host Pi runtime
access and an authenticated provider reachable through the normal runtime
mechanisms, which this delegated sandbox does not provide. The gate is left
visibly unverified by design rather than reported as a real-agent success.

## Public interfaces and fixtures added

- `tests/pirozhki_harness.py`: `PirozhkiInstance`, `build_pirozhki_instance`,
  `pirozhki_instance`, `init_git_repo`, `run_git`, `ADMIN_TOKEN`.
  `build_pirozhki_instance`/`pirozhki_instance` accept `runner_argv_prefix`
  and `seed_deterministic_configs`.
- `tests/fake_pi_runner.py`: deterministic `write_files` role field.
- `tests/e2e_pirozhki_tools.mjs`: phases `submit`, `review`, `analysis`,
  `recommend`.
- `agent_manager.analysis.RecommendationService` now persists applied profile
  recommendations to the knowledge base when one is configured.

## Remaining gaps

- The opt-in real-model gate is unverified in this environment (see above).
- T07 independent external AGY review is still required before platform
  completion; coordinator runs it after this task.
- The full suite passed once after the acceptance tests, with no further
  source changes; a second run was not needed.
- No production, deployment, push, real Telegram/LLM access or credential
  reading was used.
