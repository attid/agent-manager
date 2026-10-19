# T05D — Parallel handbook foundation: result

Status: complete (documentation only). Base commit `fc7a3f7`
(`docs: require independent external AGY review gate`) in worktree
`t05-handbook`. No code, tests, migrations, task specifications or the E2E
scenario were modified.

## What landed

A discoverable, read-only agent handbook plus the LLM entry point, built from
the committed platform contracts (T01 durable operations integrated; T02I,
T03 and T04 not yet landed).

- `llms.txt` — machine-readable entry point: platform summary, snapshot
  provenance, handbook index, discovery and verification quick references.
- `docs/handbook/README.md` — scope, the four authoritative discovery sources,
  the status vocabulary and the source-of-truth rule.
- `docs/handbook/objects.md` — Task, Conversation, Message, AgentProfile,
  ConversationRuntime, Turn, Operation; keys, links, idempotency keys.
- `docs/handbook/workspaces-and-routing.md` — `resolve_workspace`, sandbox
  `/workspace`, access policy, stored execution context, known gaps.
- `docs/handbook/profiles-runtime-skills.md` — immutable revisions, runtime
  defaults precedence, frozen execution, conversation pinning, skills,
  platform-instruction injection.
- `docs/handbook/task-lifecycle.md` — task statuses, worker, internal review
  cycle, turns, resume, recovery.
- `docs/handbook/tools-and-api.md` — full REST table, auth, error contract,
  idempotency, the 28 JS tools, discovery commands.
- `docs/handbook/review.md` — implemented automatic review cycle (including the
  current approval-by-absence heuristic gap) and the pending independent
  executor.
- `docs/handbook/analysis-and-recommendations.md` — analyzer profile, the
  pending operation executor, and the currently misleading hardcoded Telegram
  `/analyze` output.
- `docs/handbook/approvals-and-revisions.md` — optimistic revision checks and
  explicit-approval rules.
- `docs/handbook/knowledge-and-integrations.md` — knowledge layout, deferred
  write acknowledgement, skills, OpenViking, files, Telegram.
- `docs/handbook/errors-security-isolation.md` — error contract, auth
  boundary, sandbox isolation, isolated instances.
- `docs/handbook/capability-inventory.md` — 28-tool → backend → result →
  status table, executor table, worker table, truthfulness gaps.
- `docs/handbook/verification.md` — bounded checks, static probes, runtime
  checks and the T05 recheck checklist.

Existing customized identities were not touched. The handbook documents that
`ensure_default_profiles` preserves a customized `system-secretary` prompt and
that `SECRETARY_CONTRACT` is injected as an additional instruction document,
not as a replacement prompt.

## Method

The supplied canonical Codespaces skill was used before any source read:

```
ov-skill-run codespaces scripts/build_belief_map.py --root <root>
# exit 0; 90 nodes, 340 edges; wrote .belief_map.sexp (gitignored)
ov-skill-run codespaces scripts/belief_search.py layers
ov-skill-run codespaces scripts/belief_search.py analyze <module>
```

`analyze`/`boundary` output scoped the source reads to the relevant modules:
API app/schemas, storage models, secretary, operation worker/handlers,
profile/runtime, persistent conversation, knowledge, workspaces, policy.mjs.
The existing belief map/cache was preserved (only the gitignored
`.belief_map.sexp` was generated).

## Validation

Bounded repository check (the contracts this handbook documents), run from the
worktree root:

```
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_operations.py tests/test_platform_tools_contract.py \
  tests/test_api.py tests/test_api_auth.py \
  tests/test_execution_contract.py tests/test_persistent_conversation.py -q
```

Outcome: `61 passed`, no skips, exit code `0`. The Node-based
`test_platform_tools_contract.py` executed the real `policy.mjs` tools against
a temporary HTTP backend (not skipped).

Static discovery probes used while writing the handbook (all exit `0`):

```
PYTHONPATH=src .venv/bin/python -c "<create_app().openapi() paths dump>"
# 24 path operations, matching docs/handbook/tools-and-api.md
grep -c 'pi.registerTool' src/pi_runner/policy.mjs   # 28
grep -n 'worker.register' src/agent_manager/operation_handlers.py
# only git.commit_push and git.rollback registered
```

No full suite was run; the coordinator owns the final full suite.

## Interfaces confirmed from source

- REST: 24 operations across health, workspaces, tasks, imports,
  conversations/messages, turns, operations, agents
  (`src/agent_manager/api/app.py`).
- JS tools: 28 (`src/pi_runner/policy.mjs`).
- Operation kinds: `review`, `analysis`, `git.commit_push`, `git.rollback`;
  registered executors: only the two Git kinds
  (`src/agent_manager/operation_handlers.py`).
- Worker registration seam: `OperationWorker.register(kind, handler)` with
  `handler(context: OperationContext) -> Mapping | None`.
- Operation statuses: `accepted`/`pending`, `running`,
  `succeeded`/`failed`/`interrupted` (`src/agent_manager/storage/operations.py`).
- Turn statuses: `pending`, `running`, `completed`, `failed`
  (`src/agent_manager/storage/models.py`).

## Handoff — contracts T05 must recheck after integration

1. **Operation idempotency (T02I):** the JS `operationKey` currently hashes
   `(kind, target, payload)`, so a later distinct request with the same
   payload is suppressed forever. Only retries of one invocation may share a
   key.
2. **Operation authorization (T02I):** `POST /operations` must inspect the
   requested kind and target; scoped reads must not enumerate unrelated
   operations; revocation must never reopen anonymous access.
3. **Explicit instance settings (T02I/T05):** no hardcoded backend/OpenViking
   address or embedded token fallback; RunnerConfig must pass explicit paths
   and backend settings into bootstrap, workers and child Runner.
4. **Review executor (T03):** register a real `review` executor; remove the
   approval-by-absence heuristic
   (`task_worker._run_review_cycle`); require an explicit verdict; persist
   report/metrics/diagnostics; do not skip review because a task execution is
   already completed.
5. **Analysis executor (T04):** register a real `analysis` executor; replace
   the hardcoded Telegram `/analyze` findings; persist evidence-linked
   recommendations with target identity and expected revision/hash; apply only
   after explicit approval with a stale-conflict check.
6. **Capability discovery (T05):** machine-readable inventory and executable
   parity checks over OpenAPI + registered tools; inject a short platform
   operating prompt on every run/resume without replacing customized identity
   prompts; filter executable capabilities by actual permissions while keeping
   full documentation discoverable.
7. **Truthful tool results (T05):** `archive_agent`, `ask_clarification`,
   `save_project_summary`, `install_skill`, `create_skill` and
   `update_component_rules` return free-form text instead of a structured
   status.
8. **Deferred writes (T05):** acknowledge short/session writes without
   replaying or reverting newer state.
9. **Skills/knowledge:** the freeze factory reports `skills_connected=false`
   and `knowledge_connected=false`; do not claim they are loaded unless the
   integration actually connects them.

## Changed files

```
llms.txt
docs/handbook/README.md
docs/handbook/objects.md
docs/handbook/workspaces-and-routing.md
docs/handbook/profiles-runtime-skills.md
docs/handbook/task-lifecycle.md
docs/handbook/tools-and-api.md
docs/handbook/review.md
docs/handbook/analysis-and-recommendations.md
docs/handbook/approvals-and-revisions.md
docs/handbook/knowledge-and-integrations.md
docs/handbook/errors-security-isolation.md
docs/handbook/capability-inventory.md
docs/handbook/verification.md
docs/plans/platform-completion/T05D-result.md
```

`docs/plans/platform-completion/T05D.md` was supplied with the task and is left
untracked; it is not part of this commit.

## Remaining gaps

- This snapshot is documentation, not the T05 executable inventory or parity
  checker, and not the final full-suite/E2E verification.
- All `pending`/`partial` items above await T02I, T03, T04 and T05.
- The handbook is English while several existing docs are Russian; no existing
  doc was rewritten, so the two coexist by design.
