# Platform completion acceptance

Implementation and tests accepted at `b7d4d60` (source fix `e38718b`). All work is committed locally. No push, deployment, production restart or production database/knowledge write was performed. Worktrees and belief maps are preserved.

Implemented: explicit task agent/workspace routing; durable truthful operations; per-instance runtime/state and scoped execution permissions; standalone/shared strict review execution; evidence-backed analysis; explicit approval and revision-checked recommendation application; durable profile/knowledge writes; archive/restart behavior; capability inventory/parity checks; discoverable `llms.txt` and handbook; isolated deterministic and opt-in real-model acceptance.

## Verification

- Final default suite on the accepted source: **479 passed, 2 skipped**. Skips are the two opt-in real-model tests. Exact command: `PYTHONPATH=src .venv/bin/python -m pytest -q -o addopts='' --tb=short`.
- Enabled real-model acceptance: **2 passed** using production `pi_runner`, normal provider authentication and a temporary isolated instance. Exact command: `AGENT_MANAGER_REAL_MODEL_E2E=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_e2e_real_model.py -q -o addopts='' --tb=short`. See `T09P-result.md` for the latest run.
- Coordinator independently verified 84 critical integrated regressions before the final parser-only correction. AGY independently verified the final correction with 77 focused tests, 38 executor tests and 4 deterministic Pirozhki tests; source and working tree remained unchanged.
- Independent external AGY review converged: five initial findings fixed and independently confirmed; one later Markdown parser P2 fixed and independently confirmed. Final verdict **APPROVED / NO NEW P1 OR P2**, with no P3 findings in the final delta. Reports: `docs/drafts/agy_platform_pre_review.md`, `agy_platform_final_round1.md`, `agy_platform_final_round2.md`.

## Acceptance limits

The real lifecycle exercises a separate durable review and accepts either valid domain verdict. It disables automatic multi-round fixes for that run. Native-session automatic review/fix/resume is covered by deterministic subprocess tests, not by a green real automatic-loop run. A green real acceptance confirms the tested API/agent boundaries, not that a nondeterministic model always produces correct project code or an approved review.

Existing customized profiles must expose their intended tools explicitly; permissions are not silently widened. Missing external integrations return diagnostics rather than apparent success. Production adoption remains a separate user-authorized step.
