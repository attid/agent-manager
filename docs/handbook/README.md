# Kuzya agent handbook

This directory is the discoverable, read-only operating contract for agents
(secretary and system agents) and for engineers integrating with the platform.
It describes the committed behaviour of Kuzya as of base commit
`fc7a3f7`, which already contains the T01 durable operation queue.

> **Integrated snapshot.** T02I (instance isolation and scoped execution
> permissions), T03 (independent review executor), T04 (analysis,
> recommendations and approvals) and T05 (capability inventory, parity
> checks and discovery injection) are present. `src/kuzya/capabilities.py`
> is the machine-readable inventory and `tests/test_capabilities.py` checks it
> against the live OpenAPI document and the registered Pi JS tools. Source
> remains the source of truth.

## What this handbook is for

- Give an agent enough contract knowledge to call the platform tools without
  inventing endpoints, paths or success claims.
- Give an integrator the exact source files that define each boundary.
- Let a reader verify a capability instead of trusting prose.

This handbook does not replace the customized identity prompts stored in the
registry (`system-secretary`, `system-code-reviewer`, `system-log-analyzer`,
`kuzya-developer`). Those identity prompts are data and must remain
intact. The platform operating contract is injected as an additional
instruction file, not as a replacement prompt (see
[Profiles, runtime and skills](profiles-runtime-skills.md#platform-operating-instruction-injection)).

## How to discover capabilities

There is no separate registry of capabilities. Read them from the four
authoritative sources, in this order:

1. **REST surface** — `src/kuzya/api/app.py` and the generated
   OpenAPI document:

   ```bash
   PYTHONPATH=src .venv/bin/python -c \
     "from kuzya.api.app import create_app; \
      app=create_app('sqlite:////tmp/probe.db'); \
      spec=app.openapi(); \
      [print(m.upper(), p) for p in sorted(spec['paths']) \
       for m in sorted(spec['paths'][p])]"
   ```

   No database file is opened at import; `create_app` only records the URL and
   the real engine is created in the application lifespan.

2. **Registered JS tools** — `src/pi_runner/policy.mjs`. Each
   `pi.registerTool({name, parameters, execute})` call is one tool the model
   can call. The `parameters` TypeBox object is the schema.

3. **Tool exposure policy** — an agent only receives the tools listed in
   `runner_config.tools` for its current `AgentProfile` revision. The default
   secretary tool list is in `src/kuzya/knowledge.py`
   (`ensure_default_profiles`, `sec_tools`).

4. **Executable parity tests** — `tests/test_capabilities.py` compares the
   machine-readable inventory in `src/kuzya/capabilities.py` with the
   live OpenAPI document and the real tools registered by `policy.mjs` (via
   `tests/policy_tool_names.mjs`). `tests/platform_tools_contract.mjs` runs the
   real `policy.mjs` tools against a temporary HTTP backend, and
   `tests/test_platform_tools_contract.py` drives that script. Together they
   prove the inventory ↔ API ↔ JS tool contract end to end.

## How to verify completion

Two levels:

- **Repository checks** (deterministic, no model): the bounded pytest set and
  the Node parity script in [Verification](verification.md).
- **Runtime checks** (isolated instance only): read an operation back with
  `GET /operations/{id}` and inspect `status`/`result`/`error`; read a task
  back with `GET /tasks/{id}`. A submitted request is never proof of a
  completed side effect.

## Status vocabulary

All tools and API clients must distinguish these outcomes:

| Value | Meaning |
|---|---|
| `applied` | A write request reached the API and did not fail. |
| `pending` | A deferred write was queued in the session and applies after the turn. |
| `ok` | A read succeeded. |
| `failed` | The call failed; `http_status` and `error` carry the diagnostic where available. |
| `unknown` | A write outcome is unknown (transport error or server error). Read state before retrying. |

Operation protocol statuses (`accepted`/`pending`, `running`,
`succeeded`/`failed`/`interrupted`) are separate from domain verdicts such as
`approved`/`changes_requested`, which live inside an operation `result`.

## Files

| File | Covers |
|---|---|
| [objects.md](objects.md) | Durable entities, keys and links. |
| [workspaces-and-routing.md](workspaces-and-routing.md) | Workspace resolution, `agent_id`, sandbox paths. |
| [profiles-runtime-skills.md](profiles-runtime-skills.md) | Immutable profiles, runtime defaults, frozen execution, skills. |
| [task-lifecycle.md](task-lifecycle.md) | Task statuses, execution worker, turns, resume, recovery. |
| [tools-and-api.md](tools-and-api.md) | REST endpoints, JS tools, auth, idempotency, discovery. |
| [review.md](review.md) | Independent review executor and its verdict contract. |
| [analysis-and-recommendations.md](analysis-and-recommendations.md) | Analyzer evidence package, recommendation lifecycle, approval/apply. |
| [approvals-and-revisions.md](approvals-and-revisions.md) | Revision checks and explicit approval rules. |
| [knowledge-and-integrations.md](knowledge-and-integrations.md) | Knowledge base, OpenViking, skills, files, Telegram. |
| [errors-security-isolation.md](errors-security-isolation.md) | Error contract, auth, sandbox and isolated instances. |
| [capability-inventory.md](capability-inventory.md) | Tool → API → executor → status table. |
| [verification.md](verification.md) | Commands and completion checklist. |

## Source-of-truth rule

When prose and source disagree, source wins:

- REST: `src/kuzya/api/app.py`, `src/kuzya/api/schemas.py`
- Tools and permissions: `src/kuzya/capabilities.py`,
  `src/pi_runner/policy.mjs`, `src/kuzya/permissions.py`
- Operations: `src/kuzya/storage/operations.py`,
  `src/kuzya/operation_worker.py`,
  `src/kuzya/operation_handlers.py`
- Turns: `src/kuzya/secretary.py`, `src/kuzya/dispatcher.py`
- Profiles/runtime: `src/kuzya/storage/agents.py`,
  `src/kuzya/profile_runtime.py`,
  `src/kuzya/runtime_defaults.py`
- Isolation: `src/pi_runner/core.py`
