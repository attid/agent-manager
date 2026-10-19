# T10A result — isolate incoming attachments by conversation and turn

Base: `7854936` (current committed HEAD in the worktree).
Fix commit: `62ad1e54e004a55d02bafb54bb6dfad4ece9cd36`.

## What changed

Inbound Telegram media had two staging owners and wrote into a shared
`<project>/inbox` using the source basename. The Runner scans that inbox on
every launch, so a new conversation could inherit legacy files as current
attachments. The fix assigns one owner and an explicit per-turn namespace.

- `src/agent_manager/attachments.py` (new) — `ConversationAttachmentStore`
  owns staging. Under one explicit root it writes
  `<root>/<conversation-hash>/turns/<turn-hash>/files/<attachment-id><suffix>`
  plus `manifest.json`, and rebuilds a disposable
  `<root>/<conversation-hash>/views/<turn-hash>/` inbox per execution.
  Conversation, turn and attachment keys are hashes of server-side ids, and
  raw inbound paths/basenames are never used for storage. A text-only turn
  still gets an explicit empty canonical turn and empty view. Retry of the
  same turn reuses the canonical bytes when the Telegram source is gone; a
  missing declared source fails closed instead of attaching a foreign file.
- `src/agent_manager/persistent_conversation.py` — staging happens at the
  pre-launch boundary in `_prepare_execution(context, turn_input)`. The
  generated execution role receives `inbox_dir` (current view) and
  `attachments_dir` (read-only conversation store). The turn input gets an
  `attachment_context` prompt string and an `attachment_manifest` mapping.
  The old `_execute` copy into the shared project inbox is removed.
- `src/agent_manager/pi_reply.py` — `_render_task_text` renders
  `metadata["attachment_context"]` between the turn header and the user text.
  Text-only turns render `Current message attachments: none`.
- `src/agent_manager/telegram/worker.py` — the `QueueProcessor` no longer
  copies media into the shared secretary workspace inbox. It records the
  explicit Telegram media path in message metadata only.
- `src/pi_runner/core.py` — optional role fields `inbox_dir` and
  `attachments_dir`. `inbox_dir` is mounted read-only over `/workspace/inbox`
  after the project bind; `attachments_dir` is mounted read-only at
  `/attachments`. `inbox_dir` must exist and must not equal or contain the
  project.
- `src/pi_runner/cli.py` — an explicit `inbox_dir` replaces the legacy
  `<project>/inbox` scan for file arguments; the disposable view is not
  archived. The selection logic is factored into `inbox_file_args(directory)`.
- Tests and fake Runner probes updated; docs updated in
  `docs/handbook/knowledge-and-integrations.md`,
  `docs/handbook/workspaces-and-routing.md` and `docs/telegram.md`.

## Interfaces

- `ConversationAttachmentStore(root).prepare_turn(conversation_id, turn_id,
  input_message_id, metadata) -> TurnAttachments`.
- `TurnAttachments.prompt()` and `TurnAttachments.metadata()` give the child
  prompt and structured manifest.
- Handler constructor keeps a new optional keyword `attachments_root`; the
  default is `<artifact_root>/attachments`, never a host HOME path.
- Runner role JSON additions: `inbox_dir`, `attachments_dir` (both optional,
  absolute, existing directories).
- Generated turn metadata keys: `attachment_context` (string) and
  `attachment_manifest` (mapping).

## Validation

Run from the worktree with `PYTHONPATH=src .venv/bin/python -m pytest`:

```
tests/test_attachments.py tests/test_persistent_conversation.py
tests/test_telegram_bot.py tests/test_pi_runner_core.py
tests/test_pi_policy_tools.py tests/test_e2e_pirozhki.py
tests/test_execution_contract.py tests/test_isolation_permissions.py
tests/test_runner.py tests/test_secretary.py tests/test_conversations.py
tests/test_conversations_api.py tests/test_profile_runtime.py
tests/test_runtime_defaults.py tests/test_imports.py
tests/test_capabilities.py tests/test_task_worker.py
```

Result: `215 passed, 2 warnings in 35.81s` (exit code 0). The warnings are the
existing Starlette `TestClient` deprecation and a Pydantic `alias` warning;
neither is caused by this change.

New regressions prove:

- legacy `fake-image-bytes` and `fake-ogg-bytes` files really exist in the
  shared project inbox, and two fresh text-only conversations still get an
  empty current inbox, empty `inbox_files`, and a `none` attachment prompt;
- two distinct conversations keep different canonical stores and read-only
  mounts; the second cannot see the first conversation's bytes;
- two turns with the same `photo.jpg` basename keep distinct content, and the
  earlier turn is listed only as prior;
- a pre-launch failure after staging, a deleted Telegram source and an
  explicit retry reuse one canonical file byte-for-byte;
- a real attached file reaches the selected child through `/workspace/inbox`
  and the task prompt lists it;
- the Telegram worker does not copy the new media into the shared inbox;
- the Runner mounts explicit `inbox_dir`/`attachments_dir` over the legacy
  locations and `inbox_file_args` uses the selected directory.

No full suite was run: the bounded set above covers every touched boundary and
the deterministic end-to-end flow; no unresolved concern required more.

## Compatibility and remaining gaps

- Task-execution profiles that do not set `inbox_dir` keep the previous
  `<project>/inbox` behavior.
- Persistent conversations always set `inbox_dir`, including text-only turns,
  which intentionally hides the legacy shared inbox from the child.
- Existing historical files are preserved; only server-owned attachment
  store/view files are rewritten or pruned. The legacy shared inbox is not
  cleaned (the task forbids production cleanup).
- Canonical attachment files and per-turn view directories accumulate under
  the instance artifact root. They are server-owned and out of scope for this
  task; no retention policy was added.
- `attachment_manifest` is available to the handler but is not persisted to
  the database; the durable files and role-invocation task artifacts remain
  the audit trail.
