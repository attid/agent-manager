# T03 — Real independent review: result

Status: complete. Implementation commit `f62c663` on `platform/t03-review`
(base `35c3982`, T01 + permission-contract docs).

## What landed

A single, shared review implementation used by both the automatic review cycle
inside `TaskExecutionWorker` and the standalone `review` durable operation.
The reviewer is the `system-code-reviewer` profile, launched through the child
Runner protocol and the common `RuntimeDefaultsResolver` /
`profile_runtime._build_role_config` path.

- `src/agent_manager/review_executor.py` (new): `ReviewExecutor`,
  `ReviewRequest`, `ReviewResult`, `ReviewFinding`, `parse_review_report`,
  `ReviewExecutionError` / `ReviewFormatError`, and the read-only role builder.
- `src/agent_manager/operation_handlers.py`: `review_operation_handler` and
  registration in `register_default_operation_handlers`.
- `src/agent_manager/task_worker.py`: `_run_review_cycle` now delegates every
  round to `ReviewExecutor`; no more `is_approved = ... or not any(P1 ...)`.
- `src/agent_manager/operation_worker.py`: `OperationInstanceContext` gained an
  additive `runtime_defaults` field (optional, `None` by default).
- `src/agent_manager/runner.py`: composition root passes
  `RuntimeDefaultsResolver()` into the operation instance context.

### Truthfulness contract

A round is valid only when the child exited `0`, the Runner JSON reports
`status == "completed"`, the summary file exists and is non-empty, and the
report carries exactly one explicit verdict. Findings are evidence-linked:
severity plus the quoted report line and the parsed `file:line` when present.
A fenced `review` / `json` block is authoritative when present; otherwise the
canonical `ВЕРДИКТ: ОДОБРЕНО` / `ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ` (or English)
markers are required. Absent or ambiguous verdicts, `approved` with critical
findings, `changes_requested` without any finding, and malformed JSON blocks all
raise and fail the operation.

### Read-only and isolated

The reviewer role always sets `tools=["read","bash"]`,
`project_read_only=True`, `read_only=True`, drops `allow_git_commit` /
`allow_remote_write` / `allow_admin`, and adds the knowledge-base root to
`readonly_paths`. The operation loads the task directly by id, so it runs for a
task whose `execution_status` is already `completed` and never touches
`claim_in_progress`; the developer is never resumed. Manual review artifacts are
isolated under `artifact_root/task_<id>/operation_<op_id>/`.

### Persisted artifacts

Round report `review/round_N.md`, structured metrics `review/round_N.json`
(verdict, `domain_verdict`, findings, runtime provider/model/selection,
duration, returncode, timestamp) and failure diagnostics
`review/round_N_failure.json`. The operation `result` carries the same
verdict/findings/report paths, and task metadata `meta["review"]` records the
verdict or the failure diagnostics. The automatic path keeps `review_round_N.md`
and the `review/round_N.*` files under `artifact_root/<task_id>/`.

### Preserved fix/clarification workflow

`_run_review_cycle` still runs up to three rounds, resumes the developer on
`changes_requested`, and escalates to `waiting_for_input` with a clarification
after round three. A failed developer fix run now raises instead of silently
returning a "completed with remarks" summary.

## Handler registration interface

```python
# agent_manager.operation_handlers
register_default_operation_handlers(worker)
# registers:
#   OPERATION_KIND_GIT_COMMIT_PUSH -> git_commit_push_handler
#   OPERATION_KIND_GIT_ROLLBACK    -> git_rollback_handler
#   OPERATION_KIND_REVIEW          -> review_operation_handler
```

`review_operation_handler(context: OperationContext) -> dict[str, Any]` reads
`context.target_id` (task id) and `context.input["instructions"]` (optional
focus), uses `context.instance.database`, `.artifact_root`,
`.runner_argv_prefix`, `.default_workspace`, `.workspace_map`,
`.knowledge_base` and the additive `.runtime_defaults`. Its returned result:

```json
{
  "task_id": "...", "reviewer_agent_id": "system-code-reviewer",
  "verdict": "approved" | "changes_requested", "approved": true,
  "findings": [{"severity": "P1", "file": "src/x.py", "line": 9,
                "message": "...", "evidence": "P1: src/x.py:9 -- ..."}],
  "report": "...", "report_path": "...", "metrics_path": "...",
  "round": 1, "duration_ms": 12.3, "returncode": 0,
  "runtime": {"provider": "...", "model": "...", "pi_auth_provider": "...",
              "models_file": null, "selection": {"...": {"source": "...",
              "value": "..."}}}
}
```

## Validation

All commands run from the worktree with
`PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t03-review/src`
and `.venv/bin/python -m pytest`.

```
.venv/bin/python -m pytest \
  tests/test_review_executor.py tests/test_review_operation.py \
  tests/test_task_worker.py tests/test_operations.py \
  tests/test_persistent_conversation.py tests/test_runner.py \
  tests/test_execution_contract.py tests/test_imports.py \
  tests/test_platform_tools_contract.py tests/test_migrations.py \
  tests/test_concurrency.py -o addopts="" -q
```
Outcome: `114 passed`, exit code `0` (18.1s).

New tests cover: explicit approved and changes_requested verdicts, JSON-block
findings, absent/ambiguous verdict, approved-with-critical contradiction,
nonzero exit, failed Runner status, malformed stdout, empty report, missing
report file, report/metrics/failure persistence, read-only role contents,
model/provider propagation to the child, operation registration at the default
seam, review of an `execution_status=completed` task without rerunning the
developer, and the automatic task failing on malformed reviewer output. The
deterministic `tests/fake_pi_runner.py` child is used; no real model is called.

## Changed existing tests and why

- `tests/test_task_worker.py`: added `test_task_worker_malformed_review_fails_task`
  and imported `RuntimeDefaultsResolver`; the existing review test still asserts
  the same approved metrics (`verdict == "APPROVED"`, `approved is True`). No
  existing assertion was weakened. The new test proves the previously missing
  regression: malformed reviewer output must fail the task instead of the old
  "no P1 line means approved" behaviour.

## Belief map

Rebuilt with the canonical Codespaces builder:
`93 nodes, 360 edges, 750 entities`, 8 changed files re-parsed using the
incremental cache. `review_executor` is classified `other` and adds no new
architecture-boundary violation (the 7 pre-existing violations are unchanged).

## Remaining gaps

- The standalone review records the verdict in task metadata but intentionally
  does not change task status or resume the developer; a later integration task
  (T05) can act on `meta["review"]`.
- A fenced structured block is optional; reviewers that emit only free text
  must still use an explicit verdict marker. The default and seeded reviewer
  prompts request those markers.
- `OperationInstanceContext.runtime_defaults` is additive and read with
  `getattr`; T02I's permission integration must preserve or replace it when it
  reworks the instance context.
