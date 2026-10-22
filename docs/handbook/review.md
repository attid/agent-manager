# Review

There are two entry points to one implementation. Do not confuse them.

## Shared implementation

`src/kuzya/review_executor.py` owns the read-only review round:
`ReviewExecutor.execute(ReviewRequest) -> ReviewResult`. Both the automatic
task cycle and the durable operation use it, so a manual review and an
automatic review validate the same way.

The execution engine comes from the reviewer profile's
`runner_config.engine`:

- `pi` (default) runs `pi_runner run --agent <role.json> --task <prompt.md>`
  with the standard read-only Pi role and provider/model resolution;
- `agy` runs `agy --mode auto --dangerously-skip-permissions -p @<prompt.md>`
  (the prompt file, not its text) with a 600-second default timeout;
- `codex` runs `codex exec --dangerously-bypass-approvals-and-sandbox -` and
  writes the prompt to the child's stdin.

The review prompt is delivered as a file or on stdin — never as an `argv`
element — so a large diff cannot fail the launch with
`OSError: [Errno 7] Argument list too long`. Every attempt writes its own
composed prompt to `<attempt_dir>/prompt.md`; the diagnostics
`runtime.prompt_delivery` (`file` | `stdin`) and `runtime.prompt_path` record
the delivery used.

The reviewer profile's `system_prompt` from the knowledge base is embedded in
the prompt for both native engines. When it is non-empty, the executor
prepends it to the task prompt it delivers:

```
<SYSTEM INSTRUCTIONS>
{reviewer_system_prompt}
</SYSTEM INSTRUCTIONS>

{task_prompt}
```

The Pi backend appends the JSON contract to the stored reviewer instructions
(or uses the built-in default) in the reviewer role prompt file. To change reviewer instructions,
edit `agents/system-code-reviewer.md` in the knowledge base: the task worker
hot-reloads that profile before each automatic review cycle, and the next
`agy` or `codex` round receives the updated text in its prompt. An empty
`system_prompt` adds no system block to a native prompt.

## JSON result contract

All three engines must return a complete JSON object. Pi writes the JSON into
its normal summary file; native engines emit it on stdout. The executor saves
that raw result as `review_result.json`; it never scans prose, quotes or code
blocks for approval markers. Reviewers keep their existing read-only access.

```json
{
  "verdict": "changes_requested",
  "findings": [
    {
      "severity": "P1",
      "file": "src/app.py",
      "line": 42,
      "message": "Describe the defect and its correction"
    }
  ],
  "report": "Optional explanation for the reader"
}
```

`verdict` and `findings` are required. The verdict is `approved` or
`changes_requested`; findings must be an array, including `[]` for no findings.
Every finding requires severity (`P1`, `P2`, `P3`), a non-empty file and message,
and a positive integer line. Optional `evidence` and `report` are strings.
An approved result cannot contain P1; changes_requested requires a finding.
Additional fields are accepted. Counts are calculated from findings.

The JSON must occupy the entire response, without Markdown fences or prose.
The task's JSON contract supersedes older text-verdict profile instructions.
An absent, unreadable, empty or invalid result triggers a new format attempt
with the validation error. There are at most **three attempts total per round**.
After the third invalid result the review fails with persisted diagnostics;
there is no automatic approval and the developer is not resumed to fix a
format error. Nonzero process exits, launch failures, incomplete Runner status
and malformed Runner envelopes fail immediately rather than being retried.

Every invocation gets a new `round_<n>_<unique>/` artifact directory. Its
`attempt_<n>/` directories preserve prompts, raw results, stdout/stderr and
validation errors. A successful invocation saves `review_result.json` alongside
them and renders a human-readable `round_<n>.md`. Round metrics record
`result_file` and `format_attempts`; the operation result exposes `result_path`.
No previous invocation's result can satisfy a new request. The historical flat
stdout/stderr and report paths remain available for artifact readers.

Native engines are looked up on `PATH` and then in `~/.local/bin`. A missing
binary raises `ReviewExecutionError`. The domain verdict remains in the
operation result, never the operation status. The reviewer role strips
administrative keys and uses `read`/`bash` with `project_read_only=true` and
`read_only=true`.

## Automatic review inside task execution

`TaskExecutionWorker._run_review_cycle` (`src/kuzya/task_worker.py`)
runs for coding tasks with `changed_files` that did not disable review. It:

- loads the current `system-code-reviewer` profile (hot-reloading its file if
  changed);
- runs up to three review/fix rounds through `ReviewExecutor`, independently
  of the three format attempts allowed within each review round;
- persists round reports and metrics (`round_<n>.json`, including the verdict);
- re-runs the developer and re-reviews when changes are requested, and can end
  in `waiting_for_input` when the developer asks for clarification;
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

`review_operation_handler` (`src/kuzya/operation_handlers.py`) is
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

- `src/kuzya/review_executor.py`
- `src/kuzya/task_worker.py`
- `src/kuzya/operation_handlers.py`
- `src/kuzya/operation_worker.py`
- `docs/plans/platform-completion/T03.md`
