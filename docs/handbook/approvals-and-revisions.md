# Approvals and revisions

The platform keeps consequential writes explicit. Three mechanisms enforce
that: immutable profile revisions with optimistic checks, idempotency keys,
and explicit approval for identity/agent changes.

## Immutable profile revisions

- Every `PATCH /agents/{agent_id}` carries `expected_revision`. The repository
  inserts `revision + 1` only if the current maximum revision equals the
  expected value. The database enforces this atomically with
  `INSERT ... SELECT` plus the `(agent_id, revision)` primary key.
- A stale update returns `409 Conflict` and includes the observed current
  revision when known. No snapshot is modified or lost.
- Supplied fields replace the stored value whole; omitted fields are copied
  from the current snapshot. There is no deep merge.
- `create_agent` is not an upsert: an existing id returns `409`.

Agent tools must not write a profile to "check" an earlier write. Read
`GET /agents/{id}` and compare revisions instead.

## Explicit approval rules

The platform operating contract (`src/agent_manager/secretary_contract.py`)
requires:

1. Prepare a complete Markdown draft (name, id, role, prompt, tools, skills,
   `runner_config` including `project_read_only`, `network` and paths).
2. Save the draft through `write_knowledge` to `docs/drafts/*` and send it for
   approval with `attach_document`. Do not call `create_agent`/`update_agent`
   before approval and do not store the draft under `agents/`.
3. After approval of a specific version, call `create_agent` once for a new
   agent or `update_agent` once for an existing one. Report the actually
   stored revision. A conflict is resolved by the user, not by bypassing the
   check with a re-create.

The `write_knowledge` tool refuses to write into the `agents` category and
directs callers to the profile API. The session drain in
`PersistentConversationHandler` also ignores any legacy write that resolves
inside `agents/`.

## Operation idempotency and approval

- `POST /operations` with an `idempotency_key` is replay-safe. A reused key
  with a different `kind`/`target_id`/`input`/`metadata` payload returns
  `409` and preserves the original.
- A domain approval (`approved` / `changes_requested`) is stored inside the
  operation `result`. The operation `status` only reports execution
  (`succeeded` / `failed` / `interrupted`). Never treat `succeeded` as a
  domain approval without reading `result`.
- **Known gap (T02I):** the JS tools currently hash `(kind, target, payload)`
  into the key, so a later distinct request with the same payload is
  deduplicated. T02I makes only retries of one invocation share a key.

## Recommendation approvals (pending, T04)

The recommendation lifecycle is not implemented in this snapshot. The
intended rules, which T05 must recheck once T04 lands:

- proposal generation never applies the change;
- applying requires explicit approval;
- apply rechecks the expected revision/content hash and reports a stale
  conflict instead of overwriting newer content.

## Related

- `src/agent_manager/storage/agents.py`
- `src/agent_manager/api/app.py` (`/agents` endpoints)
- `src/agent_manager/secretary_contract.py`
- `src/agent_manager/persistent_conversation.py`
- `docs/plans/platform-completion/T02I.md`, `T04.md`
