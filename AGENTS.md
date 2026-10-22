# Agent Manager development

Build only the single task supplied for this run. The system specification is context, not authorization to implement every feature.

- Python 3.14 only; FastAPI/Pydantic, SQLAlchemy 2, Alembic; SQLite WAL. Telegram later uses aiogram 3.
- Keep task, conversation and execution separate. Persist state; no business rules delegated solely to prompts.
- Do not commit, push, deploy, start persistent services, or change the Runner. Do not read credentials or dump environment variables.
- Code, docs and reports use clean language. Report exact validation commands, outcomes, changed files and remaining gaps.
- Add meaningful tests for acceptance criteria. Use temporary file databases for WAL tests, not SQLite :memory:.
- Preserve validation exit codes: run tests without output pipelines, or enable `set -o pipefail` in the same shell before piping output. Report the actual command, outcome and exit code; a successful pipeline is not proof that its tests passed.
- After changes settle, run the appropriate checks once. Repeat only after new changes, a failure or a specific unresolved concern; explain the reason for additional runs.
- When changing an existing test, explain why its expectation changed and how it still detects the intended regression. Do not weaken assertions solely to make the implementation pass.
- A connection belongs to its execution context; configure foreign_keys and busy_timeout on every connection. WAL allows readers alongside a writer; writes still serialize.
- Schema changes use Alembic migrations from the first schema. Do not impose unrequested field caps or enum restrictions.
- Coordinator builds the architecture map before each task. Query the provided .belief_map.sexp before reading source and inspect only relevant boundaries. Never delete existing graph/cache data. If no graph entry exists for a new module, state that and inspect its immediate boundary only.
- For narrow tasks, use the task description and selected architecture boundaries. Read the full system specification or graph-builder cache only when a concrete question requires it; state that reason before expanding context.
- Sandbox root is /workspace. Host source directory paths are not available. Use PYTHONPATH=src when invoking project modules and tests.
- uv and Python runtime paths are supplied in the task. uv cache must be under /tmp or the role HOME. Use uv sync --no-install-project and .venv/bin/python -m pytest with PYTHONPATH=src; do not rely on a host editable-install path.
- Do not expand into future tasks. Stop and report when current acceptance criteria pass or an external blocker prevents progress.

## API and tool contract changes

- Any addition, change or removal of an HTTP route, a Pi JS tool (`src/pi_runner/policy.mjs`), a status/state, or a request/response field MUST update all three in the same change: `docs/handbook/tools-and-api.md` (method and path, path/query parameters, request body, response, error codes, idempotency), `src/kuzya/capabilities.py` (capability record with `action`/resource and `doc`) and `docs/handbook/capability-inventory.md`.
- A contract change without the matching documentation and inventory updates is a blocking defect, not deferred debt.
- Before completing a task, reconcile every METHOD+path registered in `src/kuzya/api/app.py` against `docs/handbook/tools-and-api.md` and `src/kuzya/capabilities.py`; each route must appear in all three. Run `PYTHONPATH=src .venv/bin/python -m pytest tests/test_capabilities.py -q`. If the environment cannot run it, state the exact reason in the report.
- For a new transport or adapter that proxies an existing REST surface 1:1 (for example MCP), do not duplicate every route. Add one adapter record in `docs/handbook/capability-inventory.md` stating that permissions are inherited and where that inheritance is enforced in code.
- Updating the handbook and inventory is part of the definition of done, not a separate task.
