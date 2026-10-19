# T04 — Analysis, evidence and approved recommendations: result

Status: complete. Implementation commit `89424db` (`89424db5aab0e792755690c926c1c8a9fbe96138`)
on `platform/t04-analysis`, base `35c3982`.

## What landed

- **Durable analysis executor.** `analysis_operation_handler`
  (`src/agent_manager/analysis.py`) is registered for the `analysis`
  operation kind by `operation_handlers.register_default_operation_handlers`.
  It resolves the target task, builds a read-only evidence package, runs the
  analyzer runner and persists the result. Missing task, analyzer failure,
  empty output and malformed output all raise `OperationExecutionError`, so
  the operation is recorded `failed` with a diagnostic rather than a fake
  report.
- **`system-log-analyzer` runner.** `default_analyzer_runner` freezes the
  current `system-log-analyzer` profile revision through the existing
  `freeze_profile_handler`/`PiReplyHandler` path and returns its summary
  text plus pinned revision and session provenance. The runner is injectable
  through `OperationInstanceContext.analyzer_runner` for deterministic tests.
- **Bounded-by-relevance evidence package.** `build_evidence_package` reads
  only the target task, its selected executor profile revision, operations
  whose `target_id` is the task, review evidence reachable from the task and
  its metrics. Flat evidence items (`task:`, `profile:`, `operation:`,
  `review:`, `metrics:`) let recommendations cite observed facts.
- **Structured report and recommendations storage.** Additive Alembic
  `0008_analysis` creates `analysis_reports` and `recommendations`;
  `storage.AnalysisRepository` owns report creation, recommendation creation
  and the status transitions `proposed -> approved -> applied` /
  `conflict` / `failed`.
- **Retrieval API.** `GET /tasks/{task_id}/analysis`,
  `GET /analysis/{report_id}`, `GET /analysis/{report_id}/recommendations`,
  `GET /recommendations`, `GET /recommendations/{id}` (preview),
  `POST /recommendations/{id}/approve`, `POST /recommendations/{id}/apply`.
- **Secretary client and Telegram.** `BackendClient` gained
  `submit_operation`, `trigger_analysis`, `get_operation`,
  `list_analysis_reports`, `get_analysis_report`,
  `list_report_recommendations`, `list_recommendations`,
  `preview_recommendation`, `approve_recommendation` and
  `apply_recommendation`. `cmd_analyze` submits the real durable operation
  and renders its stored pending/failed/succeeded outcome; the hardcoded
  "session completed successfully" text is gone.
- **Secretary tools and prompt.** `policy.mjs` registers
  `list_analysis_reports`, `get_analysis_report`, `list_recommendations`,
  `preview_recommendation`, `approve_recommendation` and
  `apply_recommendation`. `SECRETARY_CONTRACT` documents the
  submit/read/preview/approve/apply procedure and that `conflict`/`failed`
  mean the target was not changed.
- **Apply safety.** `RecommendationService.preview` reports the current
  target state and whether the proposal is stale. `apply` requires the
  recommendation to be `approved`, re-checks the observed profile revision
  or knowledge-base content hash, records `conflict` and raises
  `RecommendationConflictError` on a stale target, and records `applied`
  with the written revision/hash on success. Generation never mutates a
  target.

## Public interfaces for T05/T06

- Storage: `AnalysisRepository.create_report`, `get_report`,
  `list_reports(*, task_id, limit, offset)`, `create_recommendation`,
  `get_recommendation`, `list_recommendations(*, task_id, report_id, status,
  target_kind, limit, offset)`, `approve_recommendation`, `mark_applied`,
  `mark_conflict`, `mark_failed`. Errors: `AnalysisReportNotFoundError`,
  `RecommendationNotFoundError`, `RecommendationStateError`.
- Domain: `analysis_operation_handler(context)`;
  `AnalysisRequest(operation_id, task_id, evidence, instructions, instance)`;
  `AnalyzerRunResult(text, agent_id, profile_revision, session_id,
  provenance)`;
  `AnalyzerRunner = Callable[[AnalysisRequest], AnalyzerRunResult]`;
  `build_evidence_package(database, task) -> dict`;
  `parse_analyzer_output(text) -> dict`; `RecommendationService(database, *,
  knowledge_base, profile_repository)` with `preview`/`approve`/`apply`/
  `status`; `RecommendationConflictError`;
  `content_hash(text)`.
- Operation kind: `OPERATION_KIND_ANALYSIS = "analysis"`.
- Seam: `OperationInstanceContext.analyzer_runner` (None -> production
  runner). `KnowledgeBase.update_component_rules(component, rules)` writes
  and commits `components/<component>/rules.md`.
- Analyzer output contract: a single JSON object with `summary`, optional
  `findings` and `recommendations` (each naming `target_kind`, `target_id`,
  `title`, `rationale`, `evidence`, `proposed_content`, optional
  `target_revision`/`target_content_hash`/`diff`). The server replaces the
  observed revision/hash with the value read from the target, never trusting
  the model.

## Validation

Final bounded run (no output pipe; exit code preserved):

```
cd /home/itolstov/Projects/mtl/AgentManager/worktrees/t04-analysis
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_analysis.py tests/test_operations.py tests/test_migrations.py \
  tests/test_concurrency.py tests/test_conversation_runtime.py \
  tests/test_imports.py tests/test_api.py \
  tests/test_platform_tools_contract.py tests/test_pi_policy_tools.py \
  tests/test_persistent_conversation.py tests/test_runner.py \
  tests/test_profile_runtime.py tests/test_knowledge.py
```

Outcome: `133 passed, 1 warning in 18.65s`, exit code `0`.

`tests/test_analysis.py` (22 tests) covers: parser acceptance/rejection,
bounded evidence with unrelated task/operation/profile absent, real
operation-to-executor dispatch, the production `system-log-analyzer` path
through `fake_pi_runner.py`, missing task, analyzer exception/empty/malformed
failures, explicit approval, generation-does-not-mutate, exact-target apply,
stale approval conflict without overwriting newer content, project content
hash conflict, API workflow, secretary-client reachability and truthful
`/analyze`. `tests/platform_tools_contract.mjs` executes the new JS tools
against the live temporary backend. `node --check` and the belief-map rebuild
after the structural change also pass.

## Changed existing tests and why

- `tests/test_migrations.py`, `tests/test_concurrency.py`,
  `tests/test_conversation_runtime.py`: the schema head moved from
  `0007_operations` to `0008_analysis` and the model/metadata set gained
  `analysis_reports` and `recommendations`. The assertions still verify a
  single head row, no model/migration drift and a complete migration.
- `tests/test_telegram_bot.py::test_cmd_analyze`: the old expectation
  asserted the fabricated analyzer findings and re-displayed the developer
  summary. The test now asserts that `/analyze` submits a durable operation
  and reports its real status, and that neither the fabricated success text
  nor the developer summary is presented as an analyzer report. It still
  detects the regression it was written for: `/analyze` claiming success
  without a real analysis.

## Remaining gaps and seams

- **Tool exposure is still T02's scope.** `pi_runner/cli.py` force-appends
  the platform tool list, so the `system-log-analyzer` profile's
  `tools: ["read", "bash"]` is not yet an enforcement boundary; the new
  recommendation tools are added to that list so they reach the secretary.
  T02's profile-scoped exposure must keep analysis read-only and remove
  mutation tools from the analyzer. The analysis executor itself has no apply
  or write path.
- **Review evidence is provider-shaped.** `_collect_review_evidence` reads
  review operations targeting the task and review entries in task metadata.
  T03's review executor can persist its report so analysis sees it without
  this module importing T03 internals.
- **Default analyzer runner is not covered by a real-model test**, per the
  task's no-real-model rule; it is covered end-to-end with the deterministic
  `fake_pi_runner.py` fixture and the profile-freeze path.
- As the README and the worker docstring already note, abandoned-running
  recovery remains explicit and manual.

## Integration note (T05I)

T04 was integrated after T02I. The additive migration was linearized as
`0009_analysis` with `down_revision = 0008_operation_owner` (the tables and
repository are unchanged). The analysis/recommendation routes are mapped to
`analysis.read` / `analysis.approve` resources, the six tools are exposed
through the secretary profile only, and the analyzer receives the handbook
discovery as an extra instruction. See `T05I-result.md`.
