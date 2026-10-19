# T12I result — combine inbound and outgoing attachment fixes

Base: `7854936`.
Integration HEAD tested before this report: `5ebd0e0`.

## Integrated commits

Cherry-picked in the required order:

- `e722d2c` — T10A `fix(attachments): isolate inbound media by conversation and turn` (source of `62ad1e5`).
- `2c94104` — T10A documentation (source of `4d0321c`).
- `81a0d0b` — T11O `fix(conversation): scope outgoing attachments to their turn` (source of `a6b77f9`).
- `d058ea9` — T11O documentation (source of `6c966ca`).
- `5ebd0e0` — combined integration regression tests (new commit).

The two implementation commits touched the same boundaries
(`persistent_conversation`, `pi_reply`, `pi_runner/cli`, the fake Runner and
the attachment handbook section). Git reported conflicts in
`docs/handbook/knowledge-and-integrations.md`, `tests/fake_pi_runner.py` and
`tests/test_persistent_conversation.py`; they were resolved by keeping both
contracts rather than choosing one side:

- the child gets only the current turn's files as `/workspace/inbox` and the
  conversation store read-only at `/attachments`;
- outgoing `attach_document` records are drained only for the current turn,
  acknowledged after durable reply persistence, and legacy records are never
  replayed;
- the fake Runner exposes both the media probe and the attachment-request
  probe;
- both test suites remain in `tests/test_persistent_conversation.py`, with
  T10A cases followed by T11O cases.

No application behavior was changed during conflict resolution beyond
combining those contracts.

## Verified behavior

- A text-only or reset conversation receives an explicit empty current
  attachment context and an empty `/workspace/inbox`; the legacy shared
  `project/inbox` is not scanned.
- Current media is staged once under a conversation/turn-hashed store and
  mounted read-only for the selected child. Two conversations mount distinct
  stores; a second conversation cannot see the first conversation's bytes.
- Two turns with the same basename keep distinct content; retry of the same
  turn reuses the canonical bytes after the original source disappears.
- `--turn-id` reaches the real Runner on both `run` and `resume`;
  `pi/attachments.json` carries the current turn identity, and a resume with
  a different identity replaces the marker while a launch without one clears
  it.
- `attach_document` records invocation and turn identity. Only current-turn
  requests are drained; receipts are acknowledged after `finish_call`; failed
  ownership or database writes leave requests pending; legacy records and
  unrelated resumed turns do not replay; a later deliberate request for the
  same target sends again.
- Configured project/workspace paths and historical shared-inbox files remain
  intact outside the sandbox.

## Validation

All Python commands used:

```bash
PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t12-attachment-integration/src \
  .venv/bin/python -m pytest -o addopts='' -q --tb=short <paths>
```

Observed results, without hiding exit codes:

1. Bounded inbound/outgoing set:
   `tests/test_attachments.py tests/test_persistent_conversation.py
   tests/test_pi_reply.py tests/test_session_operations.py` — 65 passed,
   exit 0.
2. Runner/policy/Telegram set:
   `tests/test_pi_policy_tools.py tests/test_telegram_bot.py
   tests/test_pi_runner_core.py tests/test_secretary.py
   tests/test_task_worker.py tests/test_execution_contract.py
   tests/test_isolation_permissions.py` — 91 passed, exit 0.
3. Combined integration regression test:
   `tests/test_pi_integration.py tests/test_pi_policy_tools.py` — 12 passed,
   exit 0. The new real-bubblewrap case runs an actual sandbox with a
   fixture child, confirms that legacy `project/inbox` files are hidden,
   current media is visible at `/workspace/inbox`, the conversation store is
   visible only at `/attachments`, and a second conversation's store is not
   visible.
4. Final full suite after integration:
   `PYTHONPATH=.../src .venv/bin/python -m pytest -o addopts='' -q --tb=short`
   — `501 passed, 2 skipped, 1 warning in 55.40s`, exit 0. The skipped tests
   require the managed real Pi installation/model fixture; no model, network
   or production access was used.
5. `node --check src/pi_runner/policy.mjs` and
   `node --check tests/policy_deferred_tools.mjs` — exit 0.
6. `git diff --check` — clean.

No deployment, production HTTP/state/service/cleanup, push, secrets or
nested delegation was performed. The full suite is authorized only for this
integration task.

## Interfaces

- `ConversationAttachmentStore.prepare_turn(...) -> TurnAttachments` and
  `TurnAttachments.prompt()/metadata()` remain the inbound staging contract.
- Runner role fields `inbox_dir` and `attachments_dir` are explicit
  per-execution paths; persistent conversations set both.
- `pi_runner` accepts `--turn-id` on `run`/`resume` and publishes it as
  `<state>/sessions/<session>/pi/turn.json`.
- `policy.mjs attach_document` records `target`, `timestamp`,
  `invocation_id`, and `turn_id`.
- `session_operations.pending_items(path, match=...)` and
  `acknowledge_items(path, items)` provide turn-scoped draining and durable
  acknowledgement.
- `PersistentConversationHandler` consumes only current-turn attachment
  requests and appends `[SEND_FILE: <target>]` after the child reply.

## Remaining limitations

- Exact-once Telegram network delivery is not added. The fix removes causal
  stale-request replay across turns; Telegram send failures still follow the
  existing worker error handling.
- Canonical attachment files and per-turn views accumulate under the instance
  artifact root. They are server-owned; no retention policy was requested or
  added.
- The historical shared `project/inbox` is not cleaned or moved, by design.
  It is simply no longer the child's current inbox for persistent
  conversations.
- A first launch that fails before binding a Runner session id can leave
  unacknowledged attachment requests in the failed session. A retry that
  resumes the same session can deliver them; a new session treats them as
  audit history.
- A manual `pi_runner run/resume` without `--turn-id` records attachments
  without a turn identity, which the persistent handler intentionally ignores.
