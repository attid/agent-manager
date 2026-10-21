# Kuzya

Task-oriented agent orchestration system and engineering platform. This repository contains:
- **Durable Storage**: File-backed SQLite with WAL mode, Alembic migrations, task repository, conversation/message storage, and immutable versioned agent-profile repository.
- **Task Worker & Sandbox**: Background `TaskExecutionWorkerPool` with strict workspace isolation running tasks inside Bubblewrap sandbox via `pi-runner`.
- **Automated Code Review**: Multi-round independent reviewer (`system-code-reviewer`) with hot-reloadable prompts and metrics persisted in task artifacts.
- **Git Engine**: Atomic explicit file staging (no `git add .`), rollback capability, and commit & push with GitHub Actions CI monitoring (`gh run watch`). A repository without workflows is treated as successfully pushed; strict CI enforcement is opt-in via `require_ci`.
- **Telegram Secretary & Controller**: Interactive Telegram bot (aiogram 3) supporting text, voice transcription (Deepgram ASR), and inline confirmation cards.
- **OpenViking Integration**: Long-term memory search and reading for agents.
- **Knowledge Base**: Versioned Git-backed markdown repository for project summaries, component rules, and agent profiles.
- **REST API**: FastAPI server exposing full control over tasks, conversations, turns, and agent profiles.

## Requirements

- Python `>=3.14,<3.15`
- `uv` for dependency management

The storage layer uses SQLAlchemy 2 and Alembic, the HTTP API uses FastAPI
and uvicorn. Tests use pytest and FastAPI's `TestClient` (httpx).

## Setup

Install dependencies and create `uv.lock` without installing the project
itself:

```bash
UV_CACHE_DIR=/tmp/uv-cache UV_PYTHON_DOWNLOADS=never \
  UV_PYTHON=/home/itolstov/.local/share/uv/python/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14 \
  /home/itolstov/.local/bin/uv sync --no-install-project
```

> The runtime path above is the interpreter available in this environment.
> `uv` reports `CPython 3.14.7`; the lockfile pins the dependency versions.

## Database migrations

Apply all migrations to a fresh (or already migrated) file database:

```bash
KUZYA_DB_URL=sqlite:////tmp/kuzya.db \
  PYTHONPATH=src .venv/bin/alembic upgrade head
```

Re-running the command is safe: Alembic records the applied revision in
`alembic_version` and skips it. The URL is resolved from the
`sqlalchemy.url` Alembic option first, then from the `KUZYA_DB_URL`
environment variable. A file-backed URL is required; in-memory SQLite is not
supported because separate connections would not share state.

The API never creates or modifies schema. Start it only against a database
that has already been migrated to head; otherwise startup fails with a clear
error instead of silently creating or replacing tables.

## Task execution concurrency

`TaskExecutionWorkerPool` runs independent `TaskExecutionWorker` loops in one
process. The pool size is configured by `RunnerConfig.task_concurrency` or the
`KUZYA_TASK_CONCURRENCY` environment variable and defaults to `3`; the
value must be an integer from `1` to `32`. `int()` accepts surrounding
whitespace, so `" 3 "` is valid.

Each worker gets a unique id of the form `task-worker-<uuid>-<index>` and
claims tasks through `TaskRepository.claim_in_progress()`. Claims run under a
process-local lock and an immediate SQLite write transaction, so:

- tasks in different explicit workspaces execute in parallel;
- tasks in one explicit workspace execute strictly sequentially;
- tasks without a workspace form one serialized group, because they resolve to
  the same default or profile project directory;
- concurrent workers cannot claim the same task or the same workspace group;
- a task whose `metadata.execution_status` is already `running` is not
  claimable again, and `PATCH /tasks/{id}` with `status=in_progress` preserves
  that marker and returns `409 Conflict`.

Workspace names are the grouping key. Distinct names that a caller maps to the
same filesystem directory are not automatically merged into one execution
group.

`TaskExecutionWorkerPool.stop(timeout=...)` requests shutdown for every worker
and waits up to the shared timeout for active executions to finish. The service
shutdown path grants five seconds per configured worker
(`5 * task_concurrency`), so active parallel sessions are not squeezed into a
single five-second window.

```bash
KUZYA_TASK_CONCURRENCY=5 PYTHONPATH=src .venv/bin/python -m kuzya.runner
```

## HTTP API

Build the application factory with a database URL, or omit the argument and
set `KUZYA_DB_URL`:

```bash
KUZYA_DB_URL=sqlite:////tmp/kuzya.db \
  PYTHONPATH=src .venv/bin/uvicorn kuzya.api.app:create_app \
  --factory --host 127.0.0.1 --port 8000
```

`--factory` calls `create_app()`. The server binds loopback only
(`127.0.0.1`); do not expose it publicly.

Endpoints:

| Method | Path            | Behaviour                                            |
|--------|-----------------|------------------------------------------------------|
| `GET`  | `/health`       | Returns `{"status": "ok"}`.                          |
| `POST` | `/tasks`        | Creates a task, returns `201` with the stored record.| 
| `POST` | `/imports/tasks`| Idempotently imports history; returns created/skipped counts. |
| `GET`  | `/tasks`        | Lists tasks; optional `status`, `workspace`, `limit`, `offset` filters. |
| `GET`  | `/tasks/counts` | Returns task counts keyed by status.                 |
| `GET`  | `/tasks/{id}`   | Returns one task, or `404` when absent.              |
| `POST` | `/conversations`| Creates a conversation, returns `201` with the stored record. |
| `GET`  | `/conversations`| Lists conversations oldest first; optional `limit`, `offset`. |
| `GET`  | `/conversations/{id}` | Returns one conversation, or `404` when absent. |
| `POST` | `/conversations/{id}/messages` | Appends a message idempotently.       |
| `GET`  | `/conversations/{id}/messages` | Lists messages in stable order, or `404` when the conversation is absent. |
| `POST` | `/agents`       | Creates revision `1` of an agent profile, returns `201`. |
| `GET`  | `/agents`       | Lists the current snapshot of every agent.           |
| `GET`  | `/agents/{agent_id}` | Returns the current snapshot, or `404` when absent. |
| `GET`  | `/agents/{agent_id}/revisions` | Lists all immutable snapshots by revision. |
| `GET`  | `/agents/{agent_id}/revisions/{revision}` | Returns one snapshot, or `404` when absent. |
| `PATCH`| `/agents/{agent_id}` | Appends a revision guarded by `expected_revision`.|

`POST /tasks` accepts `title` (required), `description`, `status`
(default `incoming`, free-form), `workspace`, `metadata` (arbitrary JSON),
and `id` (defaults to a random UUID hex). The API preserves free-form
statuses and supplemental metadata and imposes no length caps. When the
caller supplies an `id` that already exists, `POST /tasks` returns `409
Conflict` with `{"detail": "task id already exists: <id>"}` instead of
erroring with a raw database failure.

### Importing completed task history

`POST /imports/tasks` accepts a batch of task records and inserts only the
ids that do not exist yet. Each item requires a stable `id` plus the same
fields as `POST /tasks` (`title` required; `description`, `status`,
`workspace`, `metadata` optional). There is no batch-size cap and no field
restriction: free-form statuses, long values and arbitrary metadata are
accepted. Extra top-level fields are folded into the stored `metadata`
(without overriding an explicit key of the same name), so execution
provenance survives import without a schema change.

Response contract (HTTP `200`):

```json
{"created": 2, "skipped": 1}
```

- `created` — tasks inserted by this request.
- `skipped` — ids that already existed, or that repeated an earlier id in
the same batch.

Import is idempotent and concurrency-safe:

- Replaying the same ids never duplicates rows and never overwrites the
  stored task (including an existing open task).
- Repeated ids inside one batch are created once and skipped afterwards.
- All records are written in one short transaction using an atomic
  `INSERT ... ON CONFLICT DO NOTHING`, so concurrent replays serialize at
  SQLite and never return `500` or create duplicates.
- Validation rejects the whole request with `422` before any write, so an
  invalid item cannot leave a partial batch.

curl example:

```bash
curl -sS -X POST http://127.0.0.1:8000/imports/tasks \
  -H 'Content-Type: application/json' \
  -d '{
        "tasks": [
          {
            "id": "run-2024-0001",
            "title": "Triage flaky test",
            "status": "done",
            "workspace": "proj-a",
            "metadata": {
              "role": "worker",
              "provider": "anthropic",
              "model": "claude",
              "runner_session": "session-42",
              "artifacts": ["s3://bucket/report.json"],
              "validation": {"passed": true},
              "usage": {"input_tokens": 10, "output_tokens": 20},
              "started_at": "2024-01-01T00:00:00Z",
              "completed_at": "2024-01-01T00:05:00Z"
            }
          },
          {"id": "run-2024-0002", "title": "Update docs"}
        ]
      }'
```

**Timestamps and provenance.** Imported tasks get `created_at` and
`updated_at` equal to import time; original execution times are not written
to those columns. Preserve original timestamps (and any other provenance)
in `metadata`, either nested under `metadata` or as top-level extra fields,
which are merged into `metadata`. Import only writes `tasks` rows; it never
launches an executor or triggers any other component.

## Conversations and messages over HTTP

Conversations and messages are exposed by thin endpoints over
`ConversationRepository`. The API imposes no length caps and no allowlists on
`role`, `text`, `source` or `metadata`; they are free-form. A conversation is
created independently and messages are scoped to it. Appending a message
never creates a task.

### Creating and reading conversations

```bash
# Create a conversation; id and timestamps are server-assigned.
curl -sS -X POST http://127.0.0.1:8000/conversations \
  -H 'Content-Type: application/json' \
  -d '{"title": "Support", "metadata": {"origin": "api"}}'

# List conversations (oldest first).
curl -sS http://127.0.0.1:8000/conversations

# Fetch one conversation (404 when absent).
curl -sS http://127.0.0.1:8000/conversations/<conversation-id>
```

### Appending messages idempotently

`POST /conversations/{id}/messages` accepts `text` (required), `role`
(default `user`), `source` (default `api`), `external_id` (optional
idempotency key) and `metadata` (arbitrary JSON). The response is the stored
message plus `replayed`:

```bash
# First delivery inserts a row (201 Created, "replayed": false).
curl -sS -X POST http://127.0.0.1:8000/conversations/<conversation-id>/messages \
  -H 'Content-Type: application/json' \
  -d '{
        "role": "user",
        "text": "hello",
        "source": "telegram",
        "external_id": "update-1",
        "metadata": {"update_id": 1}
      }'

# Listing returns messages in stable storage order (by sequence).
curl -sS http://127.0.0.1:8000/conversations/<conversation-id>/messages
```

When `external_id` is present, `(conversation_id, source, external_id)` is the
idempotency key:

- Replaying an identical `role`/`text`/`metadata` payload returns the original
  message with HTTP `200 OK` and `"replayed": true`; no new row is written and
  the stored row is never overwritten.
- Reusing the key with a different payload returns `409 Conflict` and leaves
  the original message untouched.
- `external_id` is optional: without it, every append inserts a new message.
- The same `external_id` is independent across `source` values and across
  conversations.
- Appending to an absent conversation returns `404`.
- `GET .../messages` returns `404` for an absent conversation and `[]` for an
  existing conversation that has no messages yet.

`POST /conversations` returns `409 Conflict` when a caller-supplied `id`
already exists.

## Versioned agent profiles over HTTP

The `/agents` endpoints are thin administrative wrappers over
`AgentProfileRepository`. Each agent is an immutable chain of complete
snapshots keyed by `(agent_id, revision)`; the current snapshot is the row
with the highest revision. No endpoint executes an agent, loads skills, calls
a model or exposes tools, and no schema is created at request time.

A profile carries `agent_id`, `name`, `role`, `system_prompt`,
`runner_config` (JSON object), `skills` (JSON array of binding objects) and
`knowledge` (JSON object). `name`, `role` and `system_prompt` are free-form
with no length caps, and `role` is never checked against a list. `skills`
must be an array whose members are JSON objects, matching the storage shape
that `AgentProfileRepository` persists; the binding keys themselves are
free-form and there is no skill-count limit. The API adds no product-level
field restrictions beyond the repository's structural types.

```bash
# Create revision 1.
curl -sS -X POST http://127.0.0.1:8000/agents \
  -H 'Content-Type: application/json' \
  -d '{
        "agent_id": "triage",
        "name": "Triage",
        "role": "worker",
        "system_prompt": "You triage incoming work.",
        "runner_config": {"command": "pi"},
        "skills": [{"skill": "search"}],
        "knowledge": {}
      }'

# Read the current snapshot and one revision.
curl -sS http://127.0.0.1:8000/agents/triage
curl -sS http://127.0.0.1:8000/agents/triage/revisions/1

# List the current snapshot of every agent, and one agent's history.
curl -sS http://127.0.0.1:8000/agents
curl -sS http://127.0.0.1:8000/agents/triage/revisions

# Append revision 2, changing only the system prompt.
curl -sS -X PATCH http://127.0.0.1:8000/agents/triage \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "system_prompt": "New instructions."}'
```

Contract:

- `POST /agents` always creates revision `1` and returns `201` with the
  stored snapshot. Reusing an `agent_id` returns `409 Conflict` and reports
  the current revision in the detail string; the original snapshots are
  untouched.
- `GET /agents` returns the highest-revision snapshot of every agent,
  ordered by `agent_id`. `GET /agents/{agent_id}` returns the current
  snapshot or `404`.
- `GET /agents/{agent_id}/revisions` returns every snapshot of the agent in
  ascending revision order; an unknown agent yields `[]`. `GET
  /agents/{agent_id}/revisions/{revision}` returns that exact immutable
  snapshot or `404`.
- `PATCH /agents/{agent_id}` requires `expected_revision`. The new snapshot
  is `expected_revision + 1`; omitted fields are copied from the current
  snapshot, and a supplied field replaces the stored value whole (never a
  deep merge). A body with only `expected_revision` appends an unchanged
  snapshot. `agent_id` and `revision` are not accepted in the body, so a
  patch cannot mutate identity or revision directly and such a request is
  rejected with `422`. Supplying `null` for a non-nullable scalar or object
  field is also rejected with `422`.
- A stale `expected_revision` returns `409 Conflict`, reports the current
  revision when it can be read, and leaves every stored snapshot unchanged.
  Patching an unknown agent returns `404`.
- Snapshots are durable: revisions remain readable across application
  restarts, and no migration is run on startup.

**Administrative endpoints, not a permission boundary.** This is the
loopback development prototype and the API has no authentication or
authorization. The `/agents` endpoints are administrative and are **not
automatically granted to an LLM or a Telegram client**, and they are **not a
production permission control**. Exposing the server or treating reachability
of these routes as authorization for an agent is out of scope.

## Secretary turns over the Python boundary

The one-turn secretary is a Python-only execution boundary in
`kuzya.secretary`. It persists a `turns` row for one existing
inbound `user` message, claims work atomically, calls an injected handler
**outside any database transaction**, and records either one assistant reply
or the handler error. It adds no HTTP endpoints and performs no model,
network, Telegram or supervisor work.

Turn `status` values (`pending`, `running`, `completed`, `failed`) are
execution-protocol states and are independent of the free-form `Task.status`.

### Operations

- `TurnProcessor.enqueue(*, conversation_id, input_message_id, metadata=None)`
  — validates that the input message exists, belongs to the conversation and
  has role `user`, then inserts a `pending` turn. `input_message_id` is
  unique, so replaying the call returns the stored turn with `created=False`
  and writes nothing. Wrong conversation, unknown message and non-user role
  raise focused `TurnError` subclasses.
- `TurnProcessor.claim_next(*, conversation_id=None)` — atomically claims the
  next eligible pending turn and returns it with a fresh `claim_token`, or
  `None`. A turn is eligible only when every earlier turn in its conversation
  is `completed`, so at most one turn runs per conversation while other
  conversations proceed. The conditional `UPDATE` makes concurrent workers
  safe.
- `TurnProcessor.complete(turn_id, claim_token, *, text, source="secretary",
  metadata=None)` — appends exactly one assistant message and marks the turn
  `completed` in one transaction. Repeating completion with the same token
  returns the original reply and appends nothing; a stale or different token
  raises `StaleClaimError`.
- `TurnProcessor.fail(turn_id, claim_token, error)` — marks a running turn
  `failed`, retaining the error and writing no reply. A stale token raises
  `StaleClaimError`.
- `TurnProcessor.retry_failed(turn_id)` — explicitly reopens a `failed` turn
  as `pending` (clearing the error/claim token, keeping the attempt count) so
  it can be claimed again. It is an atomic compare-and-set (`failed ->
  pending`), so a concurrent retry or claim that already advanced the turn is
  never overwritten; any other status raises `TurnStateError`.
- `TurnProcessor.recover_abandoned(turn_id, *, claim_token)` — explicitly,
  and only with the current token, returns an abandoned `running` turn to
  `pending`. There is no automatic timeout guess.
- `TurnProcessor.run_once(handler, *, conversation_id=None)` — claims one
  turn, builds a `TurnInput`, calls `handler(TurnInput) -> str` outside the
  database transaction, then completes or fails the turn. Returns
  `RunOnceResult(status="idle"|"completed"|"failed", ...)` with the stored
  turn snapshot; a handler exception is recorded on the failed row and
  returned, not re-raised.

Attempt history is durable: `attempt_count` increments on every claim and the
`created_at`, `updated_at`, `claimed_at` and `finished_at` timestamps,
`error` and `reply_message_id` are persisted on the `turns` row.

### Deterministic demo (temporary file DB, no model)

```python
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config

from kuzya.secretary import TurnProcessor
from kuzya.storage import ConversationRepository, Database

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "demo.db"
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")

    database = Database.from_path(path)
    conversations = ConversationRepository(database)
    conversations.create_conversation(id="demo")
    message = conversations.append_message(
        conversation_id="demo", role="user", text="ping", source="demo"
    ).message

    processor = TurnProcessor(database)
    processor.enqueue(conversation_id="demo", input_message_id=message.id)

    # Deterministic handler: no model, no network.
    result = processor.run_once(lambda turn_input: f"echo:{turn_input.text}")
    assert result.status == "completed"
    assert result.reply is not None

    stored = conversations.list_messages("demo")
    assert [(m.role, m.text) for m in stored] == [
        ("user", "ping"),
        ("assistant", "echo:ping"),
    ]
    database.dispose()
```

### Current limitations

- No language-model, provider SDK or prompt handling: the handler is injected
  by the caller. A real model-backed handler is future work.
- No Telegram/aiogram ingestion, dispatcher, queue, outbox or retry timer.
- No process supervisor or public worker-control HTTP endpoints. Turns are
  advanced only by explicit Python calls or `run_once`.
- Abandoned-running recovery is manual and caller-authorized; there is no
  timeout-based lease, so an operator must recover interrupted runs.

## Pi Runner reply handler (host-side adapter)

The module `kuzya.pi_reply` adapts the existing Pi Runner to the
secretary `Handler` contract (`TurnInput -> str`). It runs the Runner as a
literal-argv child process and returns the Runner's summary text, so it plugs
into `TurnProcessor.run_once` without changes to the processor, the schema or
the HTTP API. The adapter never imports `pi_runner`, never uses `shell=True`
and does not read, copy or dump credentials: the child inherits the caller's
configured environment by default.

Context is **single-message only**. Each call is a fresh LLM session; there is
no conversation-history loader, compaction, delivery outbox or automatic
retry. A failed call leaves the turn `failed` and only an explicit
`retry_failed` reopens it, which prevents accidental duplicate model effects.

### Host invocation

```python
import os

from kuzya.pi_reply import PiReplyHandler
from kuzya.secretary import TurnProcessor
from kuzya.storage import Database

handler = PiReplyHandler(
    # Trusted argv prefix: host interpreter, Runner module and PYTHONPATH.
    runner_argv_prefix=["/path/to/host/python", "-m", "pi_runner"],
    role_config_path="/abs/path/role.json",
    artifact_dir="/var/lib/kuzya/pi-runs",
    # env=None inherits os.environ unchanged; an explicit mapping replaces it.
    env={**os.environ, "PYTHONPATH": "/abs/path/to/host/src"},
)

processor = TurnProcessor(Database.from_path("/abs/path/kuzya.db"))
result = processor.run_once(handler)
if result.status == "failed":
    print(handler.last_execution.provenance_path)
```

The Runner is invoked as
`<prefix> run --agent <role_config_path> --task <task_file>`. Constructing
the handler with both `resume_session_id` and `runner_state_dir` selects an
explicit resumable call instead:
`<prefix> resume --state-dir <state_dir> --session <session_id> --agent
<role_config_path> --task <task_file>`. Supplying only one of those two
arguments is a construction-time `PiReplyConfigurationError`, so a handler
never silently falls back to a fresh session. It must print exactly one JSON
object to stdout:
`{"status": "completed", "summary_path": "/abs/path/summary.txt"}`. A
resume result must additionally carry `session_id` equal to the requested
session; missing or mismatched ids are protocol errors. Summary
text is returned only when the child exits `0`, `status == "completed"`, any
required `session_id` matches, and `summary_path` is readable, nonblank text.
Otherwise the adapter raises a typed `PiReplyError` (`PiReplyLaunchError`,
`PiReplyCommandError`, `PiReplyProtocolError`, `PiReplyFailedError`,
`PiReplySummaryError`). The error message points at the artifact directory and
never contains raw stdout/stderr or credentials.

### Artifacts and provenance

Every call writes a unique durable subdirectory (UTC timestamp + random id,
never derived from user input) under `artifact_dir`; earlier attempts are
never overwritten. `handler.last_execution` exposes the same record in memory.

| Artifact | Content |
| --- | --- |
| `task.txt` | Text passed via `--task`: `turn_id`, `conversation_id`, `input_message_id`, `attempt_count`, `role`, then the exact incoming text. |
| `stdout.txt` | Raw child stdout, UTF-8 with replacement. |
| `stderr.txt` | Raw child stderr, UTF-8 with replacement. |
| `result.json` | Parsed final stdout JSON object, when parsing succeeded. |
| `reply.md` | Immutable per-call copy of the accepted reply text; the Runner may reuse one summary source across resumed turns, so earlier replies stay intact here. |
| `provenance.json` | `run_id`, turn/conversation/input-message ids, `attempt_count`, `argv`, `launch_mode`, requested/returned session ids, timestamps, `exit_code`, `runner_status`, `outcome`, the original summary source path, the saved reply path, typed `error_type`, and every artifact path. |

`outcome` is one of `completed`, `launch_error`, `command_error`,
`protocol_error`, `runner_failed` or `summary_unavailable`. The record is tied
to the turn and attempt count, and to the artifact paths for diagnosis.

## Frozen profile handler (host-side factory)

`kuzya.profile_runtime.freeze_profile_handler` bridges the agent
registry to the existing Pi reply handler for one execution. It is a Python
host boundary only: no API endpoint, persistent worker, job loop, history
loader or model call is added. Load one revision, build a handler, pass it to
the existing `TurnProcessor`, and build a new handler for a later execution.

```python
from kuzya.profile_runtime import freeze_profile_handler
from kuzya.secretary import TurnProcessor
from kuzya.storage import Database

database = Database.from_path("/abs/path/kuzya.db")
frozen = freeze_profile_handler(
    database,
    "triage",
    # revision=None selects the latest revision at factory-call time.
    runner_argv_prefix=["/path/to/host/python", "-m", "pi_runner"],
    artifact_root="/var/lib/kuzya/profiles",
    # env=None inherits os.environ unchanged.
)

result = TurnProcessor(database).run_once(frozen)
print(frozen.agent_id, frozen.revision, frozen.snapshot_path)
print(frozen.last_execution.provenance_path)
```

Each factory call loads the chosen profile once and creates a new UUID-named
export directory below `artifact_root`. The directory contains the full
snapshot (`snapshot.json`), the exact prompt (`system_prompt.md`), the
generated Runner role configuration (`runner_role.json`) and per-execution
artifacts under `runs/`. Earlier exports are never overwritten or cleaned up.

`runner_role.json` starts from the stored trusted `runner_config`, sets the
canonical `agent_id` from the profile, and appends the exported prompt path to
the existing `instructions` list. Existing instruction entries and all other
configuration stay intact; the stored registry JSON is not modified. Runtime
tools, paths and provider references therefore keep coming from the trusted
configuration and the Runner's own validation. The factory neither reads nor
copies credentials.

`frozen` is callable with the normal `TurnInput -> str` contract and exposes
`handler` and `last_execution`. `snapshot_path`, `prompt_path`,
`role_config_path`, `export_dir`, `agent_id` and `revision` make the export
inspectable. Execution provenance records add `profile_agent_id`,
`profile_revision`, `profile_snapshot_path` and `role_config_path`, linking the
actual Runner invocation to the frozen profile and generated config.

Declarative bindings: `skills` and `knowledge` are preserved in
`snapshot.json` for traceability, but this runtime does not load, connect or
enable them. `FrozenProfileHandler.skills_connected` and
`knowledge_connected` are always `False`; `not_connected` is
`("skills", "knowledge")`. No write access is granted.

Current gaps: no profile-driven API dispatch or persistent worker, no
conversation-history loading, no skill/knowledge integration, no OpenViking
access and no automatic export retention policy. Unknown profiles or
explicit revisions raise `ProfileSnapshotNotFoundError`; malformed stored
`runner_config` raises `ProfileRuntimeConfigurationError`.

## Persistent conversation handler (host-side integration)

`kuzya.persistent_conversation.PersistentConversationHandler` is a
single host handler (`TurnInput -> str`) that connects the accepted runtime
binding repository to the existing frozen profile exports and the existing
Pi reply adapter. It adds no dispatcher, HTTP endpoint, Telegram integration,
tool API, Runner change, history loader or compaction.

The caller supplies the database, the selected `agent_id`, the trusted
`runner_argv_prefix`/`env` and the `artifact_root` for frozen exports.
Constructing the handler performs no I/O and launches no child. The first
call pins the conversation's current profile revision, builds the existing
frozen export, persists the export paths and the trusted `state_dir` from the
generated role configuration, and then runs the child in `run` mode. Later
calls reuse the same pinned role export, `state_dir` and native session id in
`resume` mode. All resume state is reconstructed from the database and
artifacts, never from handler memory, even in a brand-new process.

```python
from kuzya.persistent_conversation import (
    PersistentConversationHandler,
)
from kuzya.secretary import TurnProcessor
from kuzya.storage import Database

database = Database.from_path("/var/lib/kuzya/kuzya.db")
handler = PersistentConversationHandler(
    database,
    "triage",
    runner_argv_prefix=["/path/to/host/python", "-m", "pi_runner"],
    artifact_root="/var/lib/kuzya/conversations",
    # env=None inherits os.environ unchanged.
)

result = TurnProcessor(database).run_once(handler)
if result.status == "failed":
    print(handler.last_execution)
```

The Runner's trusted `state_dir` comes from the generated `runner_role.json`,
not from this integration. It is normalized with `expanduser`, required to be
absolute and resolved before it is persisted; a missing, non-string or
relative value raises `PersistentConversationConfigurationError` before any
child launch. A successful call must return a nonempty native `session_id`;
a missing id is a failure, never an unbound success. After a reservation is
owned, any model, protocol, export, launch or configuration failure releases
the reservation into the repository's blocked latch and re-raises the typed
error; there is no automatic replay, fresh-session fallback or unlock.

`handler.last_execution` is the adapter's `PiRunProvenance` after a fresh
child launch. When a recovered turn is satisfied from the stored binding
without a new child, it is the persisted provenance mapping instead.

Implemented: native conversation continuation with a pinned profile and a
durable native session. **Still not implemented: context budget, rotation or
summaries, and reconciliation of an uncertain model call.** This handler is
not release-ready for unbounded conversations.

## Tests

All tests use real temporary file databases, the real migration chain, and
independent connections/threads:

```bash
PYTHONPATH=src .venv/bin/python -m pytest
```

Run a single module, for example:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_concurrency.py
```

## Public Python interfaces

All storage public names are re-exported from `kuzya.storage`. The
secretary processor is exposed from `kuzya.secretary`.

### `Database`

Owns one SQLAlchemy engine bound to a single SQLite file.

```python
from kuzya.storage import Database, TaskRepository

database = Database.from_path("/tmp/kuzya.db")
repository = TaskRepository(database)
```

- `Database(url, *, busy_timeout_ms=5000, echo=False, enable_wal=True)` —
  builds the engine, applies per-connection pragmas and enables WAL on the
  file.
- `Database.from_path(path, **kwargs)` — convenience constructor for a
  filesystem path.
- `Database.session()` — context manager yielding a `Session`; commits on
  success and rolls back on error. Each call uses a fresh connection
  (`NullPool`).
- `Database.dispose()` — closes pooled connections.

### `TaskRepository`

- `create(*, title, description=None, status="incoming", workspace=None,
  metadata=None, id=None) -> Task` — inserts one task and returns the
  persisted record. `id` defaults to a random UUID hex string. A duplicate
  caller-supplied `id` raises `DuplicateTaskIdError`; other integrity errors
  propagate unchanged.
- `import_tasks(records) -> TaskImportResult` — inserts records whose `id`
  is not present and skips the rest, all in one short transaction. Returns
  `TaskImportResult(created=..., skipped=...)`.
- `get(task_id) -> Task | None`
- `list(*, status=None, workspace=None, limit=None, offset=0) -> list[Task]`
  — ordered by `created_at`, then `id`.

The repository enforces no length caps and no status allowlist. `status` is a
free string; `metadata` is arbitrary supplemental JSON.

### `ConversationRepository`

Conversations and messages are stored in their own tables and are never tied
to tasks: appending a message never creates a task. Every call opens a short
transaction on its own connection.

```python
from kuzya.storage import ConversationRepository, Database

database = Database.from_path("/tmp/kuzya.db")
conversations = ConversationRepository(database)

conversation = conversations.create_conversation(title="Support")
result = conversations.append_message(
    conversation_id=conversation.id,
    role="user",
    text="hello",
    source="telegram",
    external_id="update-1",
    metadata={"update_id": 1},
)
assert result.inserted  # a new row was written; result.replayed is False
```

- `create_conversation(*, title=None, metadata=None, id=None) ->
  Conversation` — inserts one conversation. `id` defaults to a random UUID
  hex string; a duplicate caller-supplied `id` raises
  `DuplicateConversationIdError`.
- `get_conversation(conversation_id) -> Conversation | None`
- `list_conversations(*, limit=None, offset=0) -> list[Conversation]` —
  ordered by `created_at`, then `id`.
- `append_message(*, conversation_id, role, text, source="api",
  external_id=None, metadata=None) -> MessageAppendResult` — appends one
  message. `external_id` is optional; `role` and `source` are free strings.
  Appending to an absent conversation raises `ConversationNotFoundError`.
  `role`, `text`, `source` and `metadata` have no length or allowlist caps.
- `list_messages(conversation_id, *, limit=None, offset=0) -> list[Message]`
  — ordered by the database-assigned `sequence`, never by timestamps. An
  absent conversation yields an empty list.

`MessageAppendResult` carries the stored `message` and the boolean
`replayed` (`inserted` is the negation), so a future HTTP layer can choose
between `201 Created` and `200 OK`.

#### Message idempotency

When `external_id` is present, `(conversation_id, source, external_id)` is an
idempotency key and is backed by a unique constraint:

- An exact retry returns the original message with `replayed=True` and writes
  no new row. The retry payload is semantic: `role`, `text` and `metadata`.
  Generated ids, database sequences and timestamps are excluded.
- A retry that changes `role`, `text` or `metadata` raises
  `MessageConflictError`; the original row is preserved and exposed as the
  error's `existing` attribute.
- When `external_id` is `None`, every append inserts a new message (SQLite
  treats NULL as distinct in a unique index).
- The same `external_id` may exist under a different `source` or in a
  different conversation.

The append uses an atomic `INSERT ... ON CONFLICT DO NOTHING`, so concurrent
identical delivery serializes at SQLite: exactly one row is stored and the
other deliveries return it as `replayed=True` instead of raising a raw
integrity failure. Each new message also advances the conversation's
`updated_at`; a replayed append is a no-op for `updated_at`.

### `Conversation`

SQLAlchemy ORM row for the `conversations` table:

| Column       | Type       | Notes                              |
|--------------|------------|------------------------------------|
| `id`         | string PK  | UUID hex unless supplied           |
| `title`      | string     | nullable                           |
| `created_at` | datetime   | UTC, microsecond precision         |
| `updated_at` | datetime   | UTC; advances on message append    |
| `metadata`   | JSON       | arbitrary supplemental data        |

As with `Task`, the Python attribute `Conversation.meta` maps to the
`metadata` column.

### `Message`

SQLAlchemy ORM row for the `messages` table:

| Column            | Type         | Notes                                  |
|-------------------|--------------|----------------------------------------|
| `sequence`        | integer PK   | database-assigned order key            |
| `id`              | string       | stable identifier, unique              |
| `conversation_id` | string FK    | references `conversations.id`, required|
| `role`            | string       | free-form, required                    |
| `text`            | text         | required, no length cap                |
| `source`          | string       | free-form, default `api`               |
| `external_id`     | string       | nullable idempotency key component     |
| `created_at`      | datetime     | UTC, microsecond precision             |
| `metadata`        | JSON         | arbitrary supplemental data            |

`sequence` is a SQLite rowid alias (`INTEGER PRIMARY KEY`), so SQLite assigns
it atomically on insert and it provides a stable order even when timestamps
collide. The `messages.conversation_id` foreign key is enforced on every
connection.

### `Task`

SQLAlchemy ORM row for the `tasks` table:

| Column       | Type       | Notes                              |
|--------------|------------|------------------------------------|
| `id`         | string PK  | UUID hex unless supplied           |
| `title`      | string     | required                           |
| `description`| text       | nullable                           |
| `status`     | string     | default `incoming`, free string    |
| `workspace`  | string     | nullable                           |
| `created_at` | datetime   | UTC, microsecond precision         |
| `updated_at` | datetime   | UTC, microsecond precision         |
| `metadata`   | JSON       | arbitrary supplemental data        |

The Python attribute `Task.meta` maps to the `metadata` column, because the
name `metadata` is reserved by SQLAlchemy's declarative base.

### `Turn`

SQLAlchemy ORM row for the `turns` table (execution state, separate from
tasks):

| Column             | Type         | Notes                                    |
|--------------------|--------------|------------------------------------------|
| `sequence`         | integer PK   | database-assigned order key              |
| `id`               | string       | stable identifier, unique                |
| `conversation_id`  | string FK    | references `conversations.id`, required  |
| `input_message_id` | string FK    | references `messages.id`; unique         |
| `status`           | string       | `pending`/`running`/`completed`/`failed` |
| `claim_token`      | string       | current worker token, nullable           |
| `reply_message_id` | string FK    | appended assistant reply, nullable       |
| `error`            | text         | last handler error, nullable             |
| `attempt_count`    | integer      | claims so far, default `0`               |
| `created_at`       | datetime     | UTC, microsecond precision               |
| `updated_at`       | datetime     | UTC, microsecond precision               |
| `claimed_at`       | datetime     | last claim time, nullable                |
| `finished_at`      | datetime     | last finish time, nullable               |
| `metadata`         | JSON         | arbitrary supplemental data              |

### `TurnProcessor`

The one-turn processor from `kuzya.secretary`; see *Secretary turns
over the Python boundary* for the full contract:

```python
from kuzya.secretary import TurnProcessor
from kuzya.storage import Database

processor = TurnProcessor(Database.from_path("/tmp/kuzya.db"))
processor.enqueue(conversation_id="conv", input_message_id="message-id")
result = processor.run_once(lambda turn_input: f"echo:{turn_input.text}")
```

The public operations are `enqueue`, `claim_next`, `complete`, `fail`,
`retry_failed`, `recover_abandoned`, `run_once` and the read helper
`get_turn`. Errors are `TurnError` subclasses: `TurnNotFoundError`,
`TurnInputMessageNotFoundError`, `TurnConversationMismatchError`,
`TurnInputRoleError`, `StaleClaimError` and `TurnStateError`.

## Storage guarantees

- **WAL**: the database file runs in `journal_mode=WAL`, so readers can read
  committed data while a writer holds an open transaction. WAL is a persistent
  property of the file and is enabled by `Database` and by the Alembic
  environment.
- **Per-connection pragmas**: every SQLite connection is configured with
  `PRAGMA foreign_keys=ON` and `PRAGMA busy_timeout=<busy_timeout_ms>`.
- **Bounded write waiting**: writers wait up to `busy_timeout_ms`
  (default 5000 ms) for the single SQLite writer lock before failing, rather
  than blocking indefinitely.
- **Short transactions**: repository operations open a session, perform one
  logical operation, and close it.
- **Independent connections**: the engine uses `NullPool`, and a connection
  belongs to the execution context that opened it. Independent
  connections/threads can create tasks without losing rows; SQLite still
  serializes writers.
- **Durability**: committed rows survive closing and reopening the database.

## Schema history

- `0001_create_tasks` — creates the `tasks` table and its `status` and
  `workspace` indexes. This is the first migration.
- `0002_create_conversations_messages` — adds the `conversations` and
  `messages` tables, the `messages.conversation_id` foreign key to
  `conversations.id`, the `messages.id` unique constraint, the
  `(conversation_id, source, external_id)` idempotency unique constraint and
  the `conversation_id` index. Upgrading a database created by `0001` keeps
  every existing task and only adds tables.
- `0003_create_turns` — adds the `turns` table: the unique
  `input_message_id` (idempotent enqueue), the `conversation_id`,
  `input_message_id` and `reply_message_id` foreign keys, the
  `(conversation_id, status)` index and the execution-state columns. It only
  adds a table; upgrading a database created by `0002` keeps every existing
  task, conversation and message.
