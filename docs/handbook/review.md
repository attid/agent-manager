# Review

There are two entry points to one implementation. Do not confuse them.

## Shared implementation

`src/agent_manager/review_executor.py` owns the read-only review round:
`ReviewExecutor.execute(ReviewRequest) -> ReviewResult`. Both the automatic
task cycle and the durable operation use it, so a manual review and an
automatic review validate the same way.

A review approves only when the reviewer produced an explicit, well-formed
verdict:

- a nonzero exit, a failed Runner status, an empty/malformed report, or an
  absent/ambiguous verdict raises `ReviewExecutionError` and records the
  failure; it never approves by absence of findings;
- the domain verdict (`approved` / `changes_requested`) lives in the
  operation `result` (or the round metrics), never in the operation `status`;
- findings are evidence-linked with file/line and rationale;
- the reviewer role is strictly read-only (`read` + `bash`,
  `project_read_only=true`, `read_only=true`), strips administrative keys and
  receives no administrative operation.

## Automatic review inside task execution

`TaskExecutionWorker._run_review_cycle` (`src/agent_manager/task_worker.py`)
runs for coding tasks with `changed_files` that did not disable review. It:

- loads the current `system-code-reviewer` profile (hot-reloading its file if
  changed);
- runs up to three rounds through `ReviewExecutor`;
- persists round reports and metrics (`round_<n>.json`, including the verdict);
- re-runs the developer and re-reviews when changes are requested, and can end
  in `waiting_user_input` when the developer asks for clarification;
- resumes the original developer run for the fix round: the native Runner
  session id reported by the first execution is passed to
  `pi_runner resume --session <id>` together with the same state directory,
  role/runtime and project workspace. Only when the runner reported no session
  id does the fix round fall back to a fresh `run`, so a resume is never
  attempted without the required `--session`.

The reviewer role also receives the read-only platform handbook discovery as
an extra instruction file.

## Manual trigger and standalone operation

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
operation id is the durable handle; submission is **not** a verdict. Inspect
the operation (`GET /operations/{id}`) for `status`, `result` and `error`.

`review_operation_handler` (`src/agent_manager/operation_handlers.py`) is
registered by `register_default_operation_handlers`. It loads the target task
directly, so a review still runs when the task's execution status is already
`completed`; it never reruns the developer. A failure updates the task
metadata with the diagnostics and records the operation `failed`.

## Authority and evidence rules

- Scope: the operation targets exactly one task and records the caller owner.
- The reviewer sandbox holds no backend action (`read`/`bash` grant none), so
  it cannot call the API directly.
- A review is never reported as succeeded when the report or verdict is
  missing.

## Related

- `src/agent_manager/review_executor.py`
- `src/agent_manager/task_worker.py`
- `src/agent_manager/operation_handlers.py`
- `src/agent_manager/operation_worker.py`
- `docs/plans/platform-completion/T03.md`
