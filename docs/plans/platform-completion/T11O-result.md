# T11O result — turn-scoped outgoing documents

Implementation commit: `a6b77f9` (`fix(conversation): scope outgoing attachments to their turn`).
Base: `7854936`.

## Behavior

- `attach_document` records one explicit request per tool invocation:
  `target`, `timestamp`, `invocation_id` (the Pi `toolCallId`) and the current
  `turn_id`.
- `PiReplyHandler` passes the turn identity to the Runner as `--turn-id` for
  both `run` and `resume`. The Runner writes
  `<state>/sessions/<session>/pi/turn.json` before launching Pi, and clears a
  stale marker when no identity is supplied.
- `PersistentConversationHandler` drains only attachment records whose
  `turn_id` matches the current turn. A resumed unrelated turn therefore
  cannot replay an earlier request.
- Requests are acknowledged with a durable receipt only after
  `finish_call` persisted the reply. A failed ownership check or database
  write leaves them pending.
- Repeating the same tool invocation is idempotent by `invocation_id`; a
  later deliberate invocation for the same target appends a new request and
  sends again.
- Legacy `attachments.json` records without `turn_id` remain audit history
  and are never replayed, including on the first post-upgrade turn.

## Interfaces

- `pi_runner.cli`: optional `--turn-id` on `run`/`resume`; writes
  `<state>/sessions/<session>/pi/turn.json`.
- `policy.mjs attach_document`: writes `invocation_id` and `turn_id` when
  available, deduplicated by `invocation_id`.
- `agent_manager.session_operations`: new `pending_items(path, match=...)`
  and `acknowledge_items(path, items)`; existing `unapplied_items` semantics
  are unchanged.
- `agent_manager.pi_reply.PiReplyHandler`: appends `--turn-id <turn_id>` to
  the literal Runner argv.

## Validation

All commands used `PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t11-outgoing/src`
and `.venv/bin/python -m pytest`, with no output pipeline that could hide a
failure. Results were observed directly from pytest.

1. `tests/test_session_operations.py tests/test_pi_policy_tools.py` — 8 passed.
2. `tests/test_pi_reply.py` — 25 passed.
3. `tests/test_persistent_conversation.py -k 'attachment or legacy'` — 4 passed.
4. `tests/test_persistent_conversation.py tests/test_capabilities.py tests/test_knowledge.py` — 51 passed.
5. `tests/test_pi_reply.py tests/test_session_operations.py tests/test_pi_policy_tools.py tests/test_pi_runner_core.py tests/test_task_worker.py` — 61 passed.
6. Final bounded set:
   `tests/test_persistent_conversation.py tests/test_session_operations.py tests/test_pi_policy_tools.py tests/test_pi_reply.py tests/test_capabilities.py tests/test_knowledge.py tests/test_imports.py -q`
   — 95 passed, 0 failed (only pre-existing FastAPI/Pydantic deprecation warnings).
7. `node --check src/pi_runner/policy.mjs` and
   `node --check tests/policy_deferred_tools.mjs` — passed.
8. `git diff --check` — clean.

The regressions use the real `PersistentConversationHandler` and
`TurnProcessor` against a temporary file database, temporary instance/state
roots and the real child fake Runner. The JavaScript contract calls the actual
registered `attach_document` tool.

## Compatibility and remaining gaps

- No production access, deployment, push, service control or real session
  buffer cleanup was performed.
- Exact-once Telegram network delivery is not added. The fix prevents causal
  stale-request replay across turns; a failed Telegram send still follows the
  existing worker error handling.
- If a first fresh run fails before the binding stores a Runner session id, a
  retry starts a new Runner session. Requests from the failed session remain
  unacknowledged audit history but are not visible to the new session. A
  same-session retry (the persisted resume case) keeps them pending and can
  deliver them.
- A manual `pi_runner run/resume` invocation without `--turn-id` records
  attachments without a turn identity. The persistent handler ignores such
  records by design; platform conversation launches always provide the
  identity.
