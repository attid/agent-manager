# Analysis and recommendations

> **Pending.** In this snapshot the analysis path is not functional as a real
> analyzer. The durable operation kind exists, an analyzer profile exists, but
> no executor is registered and the Telegram command returns hardcoded text.
> T04 owns the real implementation.

## Implemented: analyzer profile

`system-log-analyzer` (`src/agent_manager/knowledge.py`) is a read-only
profile:

- `role: analyzer`, `tools: ["read", "bash"]`,
  `project_read_only: true`, `network: true`;
- prompt: analyze supplied session logs/evidence, distinguish observed facts
  from hypotheses, identify missing knowledge, propose concrete updates to
  component rules or project summaries.

No execution path currently constructs a bounded evidence package for this
profile.

## Implemented: trigger submission

`trigger_analysis(task_id, idempotency_key?)`
(`src/pi_runner/policy.mjs`) submits:

```json
POST /operations
{"kind": "analysis", "target_id": "<task_id>", "input": {}, "idempotency_key": "..."}
```

The tool returns a durable `pending` operation. There is no registered
`analysis` executor, so a claimed operation fails visibly with a diagnostic
like `OperationExecutionError: no executor registered for kind 'analysis'`.
It is never reported as succeeded.

## Implemented but misleading: Telegram `/analyze`

`src/agent_manager/telegram/handlers.py` (`cmd_analyze`) reads the selected
task and then appends a fixed string:

> "💡 Выводы анализатора (system-log-analyzer): Сессия выполнена успешно.
> Проверены затронутые компоненты. Критического дефицита документации не
> выявлено."

This is not an analyzer result. Do not trust this text. T04 replaces it with a
real submit-and-read of an analysis operation. This is called out so agents do
not repeat the false claim.

## Intended contract (T04, not implemented here)

- `trigger_analysis` runs `system-log-analyzer` through the common
  runtime-resolver/profile execution path.
- The evidence package is bounded by relevance (not an arbitrary numeric cap)
  and includes: task requirements, the selected profile/revision, execution
  results/events/errors, and available review/metrics. It is read-only and
  excludes unrelated data.
- A structured report and evidence-linked recommendations are persisted and
  retrievable by the secretary and API.
- A recommendation identifies its target (profile/project/component), the
  original revision/content hash, the proposed content/diff, rationale and
  evidence, and a status.
- Generating a recommendation never mutates the target. Applying one requires
  explicit approval and rechecks the expected revision/hash, surfacing stale
  conflicts instead of overwriting newer content.
- The analyzer itself has no mutation permissions.

## Interface T05 must recheck after integration

- The `analysis` executor is registered in
  `register_default_operation_handlers` and receives
  `OperationContext` (`operation_id`, `kind`, `target_id`, `input`,
  `attempt_count`, `metadata`, `instance`).
- `OperationInstanceContext` carries the services the executor needs
  (`database`, `artifact_root`, `knowledge_base`, and the pending permission
  context).
- Recommendation storage/API routes are added and `cmd_analyze` no longer
  emits hardcoded findings.
- Approval/apply checks include an expected revision or content hash and
  return an explicit stale-conflict error.

## Related

- `src/agent_manager/operation_worker.py`
- `src/agent_manager/operation_handlers.py`
- `src/agent_manager/telegram/handlers.py`
- `docs/plans/platform-completion/T04.md`
