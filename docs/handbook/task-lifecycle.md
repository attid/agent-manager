# Task lifecycle

## Completion subscriptions

`subscribe_task(task_id="8", instructions="Summarize the findings and continue the agreed plan")`
subscribes the current conversation to one task outcome. The host supplies the
conversation identity; the model cannot redirect a subscription to another
conversation. Telegram users can also send `/watch 8` (with optional instructions)
or `/unwatch 8`. Cancellation is available until the event is queued.

The subscription is stored before returning success. On completion, failure or
a request for clarification, the service atomically appends an explicitly marked
`task_event` input and enqueues a normal secretary turn in the original dialogue.
It is not a new human instruction or approval. The secretary reads the result,
answers in Telegram and may continue only the previously authorized plan.
Polling recovers missed completion callbacks after a restart. Repeated subscription
calls are idempotent and do not generate another model call for the same subscription.

API: `POST /tasks/{id-or-number}/subscriptions` with `conversation_id` and optional
`instructions`; `DELETE` at the same path cancels a pending subscription.
The response exposes the subscription `status` and eventual `turn_id`.
Reply delivery retries separately from model execution and records per-part
progress; a failed Telegram send does not rerun the secretary. Transport delivery
is at-least-once across a crash in the send/acknowledge window.

Tasks expose a stable human `number` (`#1`, `#2`, …) alongside their internal
`id`. Display the number in Telegram and user conversations. `GET`/`PATCH
/tasks/5` resolve a number; exact internal IDs remain supported. Read the
returned `id` before submitting operations or accessing task-scoped resources.
Existing tasks are numbered in creation order by migration `0010_task_numbers`;
archival and status changes do not renumber them.

A task is a durable request for work. `TaskExecutionWorker` claims
`in_progress` tasks, runs the selected agent through the Pi Runner sandbox,
optionally runs an internal review cycle, and writes the result back to the
task row. Task `status` is free-form; the values below are the conventional
ones the worker and API use.

## Status values

| Status | Set by | Meaning |
|---|---|---|
| `incoming` | `POST /tasks`, `create_task` | Draft. Saved, not executed. |
| `in_progress` | `update_task` / `PATCH /tasks` | Authorizes execution; the worker claims it. |
| `waiting_user_input` | worker | The agent invoked `ask_clarification`; a human answer is required. |
| `ready_to_commit` | worker | Code and review completed; waits for an explicit commit/rollback decision. |
| `done` | worker or Git commit operation | Finished/committed. |
| `failed` | worker | Runtime error, failed tests or an unhandled exception. |
| `cancelled` | Git rollback operation | Cancelled or reverted. |

`incoming` does not run anything and does not schedule a future check. The
secretary must use `status="in_progress"` (or `update_task(..., status=
"in_progress")`) when the user explicitly asks to execute.

## Creating and editing

- `POST /tasks` with `id` optional; duplicate caller ids return `409`. Accepts optional `attachments` list.
- `PATCH /tasks/{task_id}` updates only supplied fields
  (`title`, `description`, `status`, `workspace`, `agent_id`, `metadata`, `attachments`);
  `metadata` replaces the whole stored object.
- `POST /tasks/{task_id}/attachments` accepts multipart/form-data upload (`file: UploadFile`), writes the file to the task artifact store, registers it in `task.metadata["attachments"]`, and returns `TaskArtifactRead` with `201 Created`.
- `POST /imports/tasks` imports historical tasks by stable id. Replaying the
  same ids never duplicates or overwrites, and validation rejects the whole
  batch before any write.
- `create_task` / `update_task` preflight the workspace and executor first
  (see [Workspaces and routing](workspaces-and-routing.md)) and copy the
  resolved `host_path` into `tasks.workspace`. Both accept optional `attachments`.

## Worker claim and workspace lock

`TaskRepository.claim_in_progress` (`src/agent_manager/storage/repository.py`)
skips a candidate when another task in the same `workspace` is
`ready_to_commit` or already `in_progress`. This serializes work per
workspace while allowing different workspaces to run in parallel.

## Execution

`TaskExecutionWorker.step` (`src/agent_manager/task_worker.py`):

1. claims one `in_progress` task;
2. selects `task.agent_id` or the worker default, loads the current profile and
   rejects archived profiles;
3. resolves the workspace again and writes the effective context to
   `task.metadata.execution_context`;
4. builds a role configuration from `runner_config`, pointing `project` at the
   resolved host directory, extracting `skills`, and enabling
   `allow_git_commit` for `devops` tasks or `metadata.allow_git_commit`;
   stages declared task attachments from host paths or artifact references into `/inbox` with read-only permissions (`0o444`), mounted via `--ro-bind` at the root `/inbox`;
5. reuses a prior native Runner session only when both the executor and the
   workspace are unchanged; otherwise it starts a new session and keeps older
   artifacts in their original directory;
6. runs the child outside any database transaction and stores summary,
   `changed_files`, errors and provenance.

## Internal review cycle

When the task has `changed_files` and review is required (coding tasks by
default; a profile/metadata flag can disable it), the worker runs up to three
review rounds with `system-code-reviewer` before marking the task. The
reviewer runs read-only against the project and writes reports to task
artifacts (`review_round_<n>.md`). The verdict strings recognized are
`ВЕРДИКТ: ОДОБРЕНО` and `ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ`. See
[Review](review.md) for the separation between this in-worker cycle and the
pending standalone review executor.

## Resume a session

`resume_task_session(task_id, message)`:

- reads the task, appends `{text, timestamp}` to
  `metadata.additional_instructions`;
- `PATCH`es the task to `status="in_progress"` with the updated metadata.

It does not itself run the model; it feeds the next worker claim, which
resumes the prior native session when the executor and workspace are
unchanged.

## Turns (secretary lifecycle)

Secretary conversations use a separate durable protocol
(`src/agent_manager/secretary.py`, `src/agent_manager/dispatcher.py`):

- `POST /conversations/{id}/turns` enqueues one `pending` turn for an existing
  inbound user message. `input_message_id` is unique, so a replay returns the
  original turn.
- `QueueDispatcher` claims one eligible turn at a time per conversation
  (`claim_next`), runs the handler outside any transaction, then completes or
  fails with the claim token.
- A terminal `failed` turn retains its error and does not block later input.
  Pending and running predecessors still enforce FIFO. Failed input is never
  replayed automatically; `TurnProcessor.retry_failed` may reopen it only before
  any later turn has started.
- A terminated Runner reporting a connection failure for the pinned resumed
  session releases its reservation for new input in that same session. Unknown
  child failures and session mismatches retain the fail-closed runtime latch;
  later input receives an error rather than waiting behind a failed turn.
- `TurnProcessor.recover_abandoned(turn_id, claim_token=...)` reopens an
  abandoned `running` turn; recovery is caller-authorized, never guessed.
- `reconcile_startup_orphans` fails `running` turns left by a terminated
  process so a conversation is never permanently blocked.

The dispatcher loop never dies from one bad turn; loop-level exceptions are
logged and polling continues.

## Operation execution permissions and discovery

- Each task execution receives an execution-scoped backend credential derived
  from its configured tools; a raw HTTP call cannot exceed that scope
  (`src/agent_manager/permissions.py`).
- Workers build an explicit per-instance context rather than reading a host
  path default (`src/agent_manager/instance.py`).
- The `review` and `analysis` operation kinds have registered executors
  (`src/agent_manager/operation_handlers.py`); a kind with no executor still
  fails visibly. See [Review](review.md) and
  [Analysis and recommendations](analysis-and-recommendations.md).
- Secretary and `system-*` executions receive the injected platform discovery
  on every run/resume without replacing the stored identity prompt (see
  [Profiles, runtime and skills](profiles-runtime-skills.md#platform-operating-instruction-injection)).

## Related

- `src/agent_manager/task_worker.py`
- `src/agent_manager/secretary.py`
- `src/agent_manager/dispatcher.py`
- `src/agent_manager/storage/repository.py`
- `src/agent_manager/storage/models.py`
