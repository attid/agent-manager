# Platform completion execution plan

User authorization (2026-10-19): implement the agreed platform plan using Pi/agy workers; independent Git worktrees are authorized; commit each completed task. This explicitly authorizes scoped Runner edits and commits despite the older AGENTS.md prohibition. No push, deployment, production service changes or production database/knowledge writes.

## Outcome
Every advertised tool reaches a working API implementation or reports a truthful diagnostic. The secretary and system agents can discover the complete platform contract. Review, analysis, approved recommendations and the Pirozhki lifecycle work on an isolated instance.

Accepted implementation, verification and limitations are recorded in [completion.md](completion.md). Independent external AGY review ended with APPROVED after the confirmed findings were corrected. Production was not updated.

## Task DAG

- T00: checkpoint the already verified routing/profile fixes and preserved E2E scenario.
- T01: durable operations, API and truthful tool lifecycle.
- T02: instance isolation and execution-scoped API/tool permissions (parallel to T01).
- T03: independent review executor and strict review acceptance (after T01).
- T04: evidence-backed analysis, recommendations and approvals (after T01; parallel to T03).
- T05: integrate T01–T04, finish all advertised operation handlers, comprehensive discoverable agent handbook and executable capability inventory.
- T06: isolated deterministic Pirozhki E2E plus opt-in real-model acceptance and final verification.
- T07: independent external AGY review of the complete committed implementation and Pirozhki acceptance; verify findings, fix confirmed defects through Pi, and repeat review before completion.

Each Task has its own file, worktree and commit(s). Worker implementations do not spawn other agents. Coordinator integrates tested commits in dependency order and independently checks results. Do not delete worktrees or maps to save space. Tests must use temporary storage; no production endpoint is permitted. Full suites run in an outer Bubblewrap namespace that substitutes the application state directory.

## Cross-task contracts

Prefer these names so parallel work can integrate without inventing incompatible protocols:

- Durable `Operation`: id, kind, target_id, input JSON, status, result JSON, error, idempotency key, attempt and timestamps. Public statuses distinguish accepted/pending/running/succeeded/failed/interrupted. Domain verdicts (approved/changes_requested) are separate from execution success.
- API `/operations`: submit and inspect operation; existing trigger tools return its ID instead of declaring success. Lost write responses are resolved by reading the same idempotent operation.
- Executors receive operation input plus a typed per-instance context; expensive model/process calls happen outside DB transactions. Review/analysis modules expose a registration/helper seam for the worker.
- Analysis produces evidence-linked recommendations with target identity and expected revision. Applying a recommendation requires explicit approval and rechecks that revision.
- Tool exposure follows profile configuration. API tokens issued to executions are restricted to intended actions/resources; a shell cannot bypass authorization by calling HTTP directly.
- Instance paths/backend settings are explicit, passed from RunnerConfig into bootstrap, workers and child Runner. No hardcoded production address/path fallback for executions.
- Existing task status remains free-form; operation protocol statuses may be constrained as protocol values. Do not introduce business caps, role/name allowlists or arbitrary field limits.

## Verification

Per task: reproduce the relevant regression first, run bounded meaningful tests, report exact commands/status and SHA. Existing schema-preservation tests must seed historical schemas using historical columns, not the latest ORM. Final: full pytest, JS syntax and actual registered JS tools against a temporary HTTP backend, deterministic end-to-end flow, negative permission and isolation tests, restart/idempotency checks. Real LLM acceptance uses an explicitly isolated configuration and never production Telegram.
