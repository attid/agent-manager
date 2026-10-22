# Objects and links

The platform stores seven durable object types. All live in one SQLite
database (`src/kuzya/storage/models.py`), all use naive UTC
timestamps, and all secondary JSON is stored under a `metadata` column
exposed in Python as `meta`.

## Entity table

| Object | Table | Identity | Purpose |
|---|---|---|---|
| `Task` | `tasks` | `id` (caller or server string) | Unit of requested work, its status, `agent_id`, `workspace` and execution metadata. |
| `Conversation` | `conversations` | `id` | Durable dialogue container. |
| `Message` | `messages` | `id` plus autoincrement `sequence` | One message in a conversation; ordered by `sequence`. |
| `AgentProfile` | `agent_profiles` | `(agent_id, revision)` | One immutable snapshot of an agent's identity and runtime policy. |
| `ConversationRuntime` | `conversation_runtimes` | `conversation_id` | Pins a conversation to one profile revision and one native Runner session; holds the model-call reservation. |
| `Turn` | `turns` | `id` plus autoincrement `sequence` | One durable secretary execution for one inbound user message. |
| `Operation` | `operations` | `id` plus autoincrement `sequence` | One durable deferred trigger (review, analysis, Git). |

## Task

`Task` fields: `id`, `title`, `description`, `status`, `agent_id`,
`workspace`, `created_at`, `updated_at`, `metadata`.

- `status` is free-form. The conventional values used by the worker are
  documented in [Task lifecycle](task-lifecycle.md).
- `agent_id` selects the executor; `workspace` selects the host directory.
  Neither is derived from `status` or `metadata.task_type`.
- `metadata` carries execution provenance (`execution_context`,
  `execution_agent_id`, `execution_workspace`, `changed_files`, `summary`,
  `error`, `additional_instructions`, `requires_review`, `task_type`,
  imported history). The claim stores the effective agent and project
  directory even when the task itself has null `agent_id`/`workspace`, so a
  direct talk cannot start in the same actual workspace.
- `metadata.changed_files` and `metadata.provenance.changed_files` are
  different fields and must not be confused:
  - the task-level `changed_files` is the list of paths the task agreed to
    change; the Git commit/rollback handlers read it as the explicit commit
    set;
  - `provenance.changed_files` is what the last Runner session itself
    reported, which can be unrelated or incomplete (an observed session
    reported `[".coverage"]` while the task held 10 correct paths).
  `GET /tasks/{id}/session` returns the task-level list first and falls back
  to provenance only when the task-level field is absent. Never treat
  provenance as the commit set, or the task-level list as the last session's
  raw observation. See
  [Verification](verification.md#git-state-inspection-read-only).
- Tasks are never created from messages. The import path
  (`POST /imports/tasks`) writes tasks by stable id and is idempotent.

## Conversation, Message and Turn

- A `Message` belongs to exactly one `Conversation` (`ON DELETE CASCADE`).
- `metadata` is opaque JSON. The Telegram direct-agent flow uses
  `talkable_agent`, `talk_active`, `talk_pending`, `talk_closing`,
  `talk_closed`, `talk_workspace` and `talk_started_at` to keep a task-agent
  dialogue and its workspace lock; these keys are not a separate object type
  and do not create tasks.
- `(conversation_id, source, external_id)` is the message idempotency key.
  SQLite treats `NULL external_id` rows as distinct, so an append without an
  `external_id` always creates a new message.
- A `Turn` links one `Conversation` to one existing inbound user `Message`
  through `input_message_id`, which is unique. Replaying an enqueue returns
  the original turn instead of writing a second one.
- Turn `status` is an execution-protocol value only: `pending`, `running`,
  `completed`, `failed`. It is unrelated to the free-form task `status`.
- `claim_token` fences completion/failure. `reply_message_id` links the
  appended assistant message. `attempt_count` and the four timestamps retain
  attempt history.

## AgentProfile

- Composite primary key `(agent_id, revision)`. Rows are never updated in
  place; `PATCH /agents/{id}` inserts `revision + 1`. The current snapshot is
  the maximum revision.
- `role` and `agent_id` are free-form strings. `runner_config` and
  `knowledge` are JSON objects, `skills` is a JSON array of binding objects.
  No role allowlist, field cap or skill-count limit is imposed.
- `runner_config.talkable: true` opts a profile into the Telegram
  direct-conversation list. A missing or non-`true` value leaves it out; the
  flag is read from the current snapshot and creates no separate profile copy.
- `CORE_SYSTEM_AGENTS = {system-secretary, system-code-reviewer,
  system-log-analyzer}` may not be archived through the API.

## ConversationRuntime

- At most one row per conversation.
- `(agent_id, profile_revision)` is a composite foreign key to
  `agent_profiles`, so a binding can never point at a snapshot that does not
  exist.
- `role_export_path`, `snapshot_export_path`, `prompt_export_path` and
  `runner_state_dir` are attached on first preparation.
- `runner_session_id` is set after the first successful model call.
- The reservation triple (`reservation_token`, `reservation_turn_id`,
  `reservation_attempt_count`) is cleared on completion or block.
- `blocked_reason` is a fail-closed latch with no automatic unlock.
- `last_turn_id` / `last_reply_text` / `last_provenance` allow recovery of a
  crash between storing a reply and completing the turn.

## Operation

- Fields: `id`, `kind`, `target_id`, `input`, `status`, `result`, `error`,
  `idempotency_key`, `claim_token`, `worker_id`, `attempt_count`,
  `created_at`, `updated_at`, `claimed_at`, `finished_at`, `metadata`.
- Protocol statuses: `accepted`/`pending` (queued), `running` (claimed by one
  worker), `succeeded`/`failed`/`interrupted` (terminal). A domain verdict
  belongs inside `result`, never in `status`.
- `idempotency_key` is nullable and unique; `NULL` keys are always distinct.
- `claim_token` fences `complete` and `fail`, so a stale worker cannot close a
  newer claim.
- Kinds currently defined: `review`, `analysis`, `git.commit_push`,
  `git.rollback` (`src/kuzya/operation_worker.py`). Only the two Git
  kinds have registered executors in this snapshot.

## Link diagram

```
Conversation 1──* Message
Conversation 1──1 ConversationRuntime ──(agent_id, revision)──> AgentProfile
Conversation 1──* Turn ──input_message_id──> Message
                          └─reply_message_id──> Message
Task.agent_id ──(soft)──> AgentProfile.agent_id
Task.workspace ──(host directory, not a stored object)
Operation.target_id ──(soft; kind-dependent)──> Task.id (review/analysis/Git)
```

Soft links are resolved by executors at run time and are not database foreign
keys, so a deleted or renamed target fails visibly inside the operation rather
than corrupting the queue.

## Related

- Storage contract: `src/kuzya/storage/__init__.py`
- Migration chain: `migrations/versions/0001`–`0009`
  (`0008_operation_owner`, `0009_analysis`)
