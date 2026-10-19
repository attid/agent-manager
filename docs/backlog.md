# Agent Manager backlog

## Sprint working agreement — 2026-10-19

For the next approximately one hour, the user requested direct coordinator edits, no subagents or external reviews, and only targeted tests. Defer slow investigation, broad test runs and independent review here. Commit completed changes. Do not deploy until explicitly requested again.

## Deferred verification

- After sprint, review task-completion subscriptions: event injection, delivery
  recovery, subscription management across `/reset`, and retrying an event turn
  interrupted during model execution. Ordinary dispatcher orphan handling still
  applies; no automatic model retries or exact-once Telegram guarantee were added.
- On the next explicitly authorized rollout, add `subscribe_task` and
  `unsubscribe_task` to the existing secretary profile, preserving its prompt
  and other tools. Default newly created profiles include them; customized
  existing profiles are not silently widened. `/watch` works independently of
  the model's tool list after deployment. Test one real secretary continuation
  then; the sprint validates queue/delivery with isolated deterministic handlers.

- Independently review attachment fixes in `7854936..3a1ca60` after the sprint. The review was started before the sprint and interrupted at the user's mode change; no final verdict is claimed. Existing validation before sprint: 501 tests passed, 2 opt-in tests skipped; coordinator independently ran 46 relevant tests successfully, including real Bubblewrap isolation. Preserve `/tmp/am-agy-attachment-review-round1.log` and existing task reports. Do not repeat the full suite during the sprint.
- Before a later rollout, verify the two reported user flows: reset then text greeting without stale attachments; send a document, then an unrelated image question without document replay. Attachment source changes are committed locally; production remains on `7854936`.

## Confirmed follow-ups

- Provider connection failures: the production secretary run at
  `20261019T145052073358Z-e85e8c0127aa42a186c9ec40ce697608` ended with
  `Connection error.` after Pi's automatic retry. Safe diagnosis and concise
  Telegram error display are fixed in source. Follow-up source changes remove
  terminal failed turns as FIFO blockers and preserve the same native session
  after a confirmed terminated connection failure on resume. Unknown outcomes
  remain fail-closed. Validate this recovery with a real provider after an
  explicitly authorized rollout; no blind model retries were added. Existing
  production blocked conversations are not repaired by a source-only fix.

- Investigate the separate production execution failure for internal task
  `154b7a1d4bed467689cfd6f59981fcc3` (Runner exited 1 on 2026-10-19).
  The discussion-button HTML error is fixed independently; it is not the
  Runner failure. No production state was changed during the sprint fixes.
- Include human-number lookup in later scoped-permission review. Telegram and
  secretary operations resolve the number to the internal ID; numeric aliases
  must never bypass resource checks. Broad review remains deferred for sprint.

- Per-task read-only execution: currently project access is inherited from the agent profile. Add an explicit task restriction through API/tools, effective sandbox policy and preflight/provenance without editing the shared profile. A task restriction must not broaden a read-only profile's privileges. Not implemented yet.
- Skills capability reporting: freeze handler hardcodes `skills_connected=False`, while explicit `runner_config.skills` is passed to Pi with `--skill` and the instance skills directory is mounted. Reconcile reporting/docs and normalize declarative `AgentProfile.skills` consistently across execution paths. Scout revision 34 was observed with `codespaces` selected and installed; actual skill use in an execution was not verified. Do not describe all skills as nonfunctional.
