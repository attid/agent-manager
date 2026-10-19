# Analysis and recommendations

Analysis is a durable operation with a real executor. Submission is not a
result; read the operation, the report and its recommendations.

## Analyzer profile

`system-log-analyzer` (`src/agent_manager/knowledge.py`) is read-only:

- `role: analyzer`, `tools: ["read", "bash"]`, `project_read_only: true`,
  `network: true`; the profile revision is frozen per execution and receives
  no mutation tool and no platform action.

## Trigger

`trigger_analysis(task_id, idempotency_key?)` (`src/pi_runner/policy.mjs`)
submits:

```json
POST /operations
{"kind": "analysis", "target_id": "<task_id>", "input": {}, "idempotency_key": "..."}
```

The tool returns a durable `pending` operation. Inspect it with
`GET /operations/{id}`; submission alone is never a result.

## Executor and evidence

`analysis_operation_handler` (`src/agent_manager/analysis.py`) is registered
for the `analysis` kind by `register_default_operation_handlers`. It:

- resolves the target task and raises `OperationExecutionError` when it is
  missing;
- builds a read-only evidence package bounded by relevance via
  `build_evidence_package`: the task, its selected executor profile revision,
  operations targeting the task, and review/metrics evidence reachable from
  the task. Unrelated tasks are not read.
- runs the analyzer through `resolve_analyzer_runner` (the injectable
  `OperationInstanceContext.analyzer_runner`, or the production
  `default_analyzer_runner`, which freezes the pinned `system-log-analyzer`
  revision and returns text plus provenance);
- parses a strict single-JSON-object report via `parse_analyzer_output`. An
  empty, non-JSON, summary-less or malformed report raises
  `OperationExecutionError`, so the operation is recorded `failed` rather
  than producing a fake report.

The analyzer sandbox is read-only and has no platform action; it cannot apply
anything on its own behalf.

## Report and recommendations

A successful run persists an `analysis_reports` row and evidence-linked
`recommendations` rows through `AnalysisRepository`. A recommendation records
its target (`target_kind` is `profile`, `project` or `component`), the
observed target revision or content hash, proposed content/diff, rationale and
evidence, and a workflow status. The server replaces any model-supplied
revision/hash with the value it observed.

Retrieval API:

| Method | Path |
|---|---|
| `GET` | `/tasks/{task_id}/analysis` |
| `GET` | `/analysis/{report_id}` |
| `GET` | `/analysis/{report_id}/recommendations` |
| `GET` | `/recommendations` |
| `GET` | `/recommendations/{id}` (preview) |
| `POST` | `/recommendations/{id}/approve` |
| `POST` | `/recommendations/{id}/apply` |

The secretary client (`src/agent_manager/telegram/client.py`) and tools
(`list_analysis_reports`, `get_analysis_report`, `list_recommendations`,
`preview_recommendation`, `approve_recommendation`, `apply_recommendation`)
expose the same surface. They are enabled for the secretary profile only
(`sec_tools`), never added unconditionally to every execution.

## Approval and apply safety

- Generation never mutates the target; `preview` only reports the current
  target state and whether the proposal is stale (`would_conflict`).
- `approve` records intent and requires status `proposed`.
- `apply` requires status `approved`, re-checks the observed profile revision
  or knowledge-base content hash, writes, and records `applied`. A stale
  target records `conflict` and raises `RecommendationConflictError`; newer
  content is never overwritten. A profile proposal may be a full replacement
  document or, as the analyzer output contract permits, a non-empty prompt
  string (treated as `system_prompt`); an echoed server-managed
  `agent_id`/`revision` is ignored because identity comes from `target_id`
  and the revision is re-checked from `target_revision`.
- HTTP authorization maps the read routes to `analysis.read` and
  approve/apply to the management action `analysis.approve`. A task-scoped
  `analysis.read` grant cannot read another task's report by id (that needs
  wildcard authority), and `analysis.trigger` does not grant approval.
- `cmd_analyze` (`src/agent_manager/telegram/handlers.py`) shows a stored
  report without launching a model; when none exists it submits the real
  durable operation and renders its stored pending/failed/succeeded outcome.
  A repeated call while an operation is pending replays the same
  revision-bound idempotent operation, and `/analyze <task_id> refresh`
  explicitly starts a new analysis after the evidence changed. It never
  prints hardcoded findings.

## Related

- `src/agent_manager/analysis.py`
- `src/agent_manager/storage/analysis.py`
- `src/agent_manager/telegram/handlers.py`
- `src/agent_manager/operation_handlers.py`
- `docs/plans/platform-completion/T04.md`
