# Review

There are two distinct review paths. Do not confuse them.

## Implemented: automatic review cycle inside task execution

`TaskExecutionWorker._run_review_cycle` (`src/agent_manager/task_worker.py`)
runs for coding tasks that produced `changed_files` and did not disable
review. It:

- loads the current `system-code-reviewer` profile (hot-reloading its file if
  changed);
- runs up to three rounds of a read-only review against the task project;
- writes round reports to task artifacts (`review_round_<n>.md`) and the
  review directory;
- checks for the explicit verdict strings `ВЕРДИКТ: ОДОБРЕНО`,
  `VERDICT: APPROVED` and the `P1:` finding marker;
- **current approval heuristic gap:** it also approves when no line contains
  `P1`, even without an explicit APPROVED verdict. That is "approval by
  absence" and T03 must replace it with a mandatory explicit verdict;
- writes per-round metrics (`round_<n>.json`) with a `verdict` field;
- if changes are requested, re-runs the developer and re-reviews, and can end
  in `waiting_user_input` when the developer asks for clarification instead.

The reviewer profile runs with `project_read_only=true` and only `read` +
`bash` tools. This path is automatic and tied to the developer execution.

## Implemented: manual trigger submission

`trigger_review(task_id, instructions?, idempotency_key?)`
(`src/pi_runner/policy.mjs`) submits a durable operation:

```json
POST /operations
{
  "kind": "review",
  "target_id": "<task_id>",
  "input": {"instructions": "..."},
  "idempotency_key": "<optional>"
}
```

The tool returns the operation with `status` starting at `pending`. The
operation id is the durable handle; submission is **not** a verdict.

At this commit there is no registered executor for the `review` kind.
`register_default_operation_handlers` registers only
`git.commit_push` and `git.rollback` (`src/agent_manager/operation_handlers.py`).
A claimed `review` operation therefore fails visibly with a diagnostic of the
form:

```
OperationExecutionError: no executor registered for kind 'review'
```

It is recorded `failed`; it is never reported as succeeded.

## Authority and evidence rules

Review operations must never approve by absence of findings. The intended
contract (T03) is:

- a nonzero exit, failed Runner status, empty/malformed report, or absent
  explicit verdict fails the operation;
- the verdict (`approved` / `changes_requested`) is a domain value stored in
  the operation `result`, not the operation `status`;
- findings are evidence-linked (file/line plus rationale);
- the reviewer has read-only project access and no administrative
  operations.

## Interface T05 must recheck after integration

- T03 registers a real `review` executor at
  `register_default_operation_handlers` and reuses the common runtime
  resolver/profile execution path. Verify the handler signature remains
  `handler(context: OperationContext) -> Mapping | None`.
- Verify automatic and manual review share the same implementation.
- Verify a `review` operation is not ignored because the task execution
  status is already `completed`.
- Verify review results are persisted on the operation (`result`, plus report
  and metrics paths) and failure diagnostics are persisted in `error`.

## Related

- `src/agent_manager/task_worker.py`
- `src/agent_manager/operation_handlers.py`
- `src/agent_manager/operation_worker.py`
- `docs/plans/platform-completion/T03.md`
