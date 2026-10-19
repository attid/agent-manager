# Profiles, runtime and skills

An agent is data: an immutable, versioned `AgentProfile`. Execution freezes
one revision, resolves the current model/provider outside the registry, and
builds a child Runner invocation. Skills and knowledge are declarative
bindings in this snapshot; the execution factory does not load them.

## Identity and revisions

- `POST /agents` creates revision `1`. A repeated `agent_id` returns `409`
  with the current revision; create is never an upsert.
- `PATCH /agents/{agent_id}` requires `expected_revision`, copies omitted
  fields from the current snapshot, replaces supplied fields whole (no deep
  merge), and appends `revision + 1`. A stale revision returns `409` with the
  observed current revision.
- `GET /agents/{agent_id}/revisions` lists history;
  `GET /agents/{agent_id}/revisions/{revision}` reads one snapshot.
- `DELETE /agents/{agent_id}` archives (soft-deletes). Core system agents
  (`system-secretary`, `system-code-reviewer`, `system-log-analyzer`) cannot
  be archived.
- Profile writes are synchronous. The response is the stored snapshot and
  revision. Do not retry a write to check it; read it back instead.

## Default system identities

`src/agent_manager/knowledge.py` (`ensure_default_profiles`) seeds and
maintains:

| `agent_id` | Role | Purpose |
|---|---|---|
| `system-secretary` | `secretary` | Telegram-facing platform controller. |
| `agent-manager-developer` | `developer` | Default coding executor. |
| `system-code-reviewer` | `reviewer` | Read-only code reviewer. |
| `system-log-analyzer` | `analyzer` | Read-only session/log analyzer. |

This seeding never overwrites a customized identity prompt. If a user-
customized `system-secretary` prompt exists, it is preserved
(`prompt_to_keep = secretary.system_prompt`). The profile file
`agents/<agent_id>.md` in the knowledge base is the on-disk mirror; database
state and files are synchronized by `sync_agents_from_files`,
`sync_agent_from_file` and `sync_agents_from_db`.

## Runtime defaults

Provider/model selection is deliberately **not** stored as the profile's
durable truth. `src/agent_manager/runtime_defaults.py` resolves it per
execution, with field-level precedence:

1. explicit `provider` / `model` / `pi_auth_provider` / `models_file` in the
   trusted role configuration (highest);
2. an optional caller-selected TOML file with a `[default]` table;
3. host global Pi settings, `~/.pi/agent/settings.json`, `defaultProvider` /
   `defaultModel`.

Behaviour:

- Resolution is late-bound: editing a default file affects the next
  execution without touching the registry or restarting.
- Precedence is per field, so `provider` can be overridden while the rest is
  inherited. A profile that defines both `provider` and `model` is a complete
  override and the default files are not read.
- `pi_auth_provider` follows the selected `provider` when not explicit. A
  selected `model` without a provider or explicit `pi_auth_provider` raises
  `RuntimeDefaultsConfigurationError` instead of guessing.
- The result is inspectable evidence (`values`, `selection`,
  `app_config_path`, `global_settings_path`, copied `role_config`). Credentials
  are never read and never serialized.

## Frozen execution

`src/agent_manager/profile_runtime.py` freezes one revision for one run:

```
<artifact_root>/<uuid>/
    snapshot.json        full profile snapshot and declarative bindings
    system_prompt.md     exact stored system prompt
    runner_role.json     generated Runner role configuration
    runs/                per-execution artifacts
```

- Returns a callable `FrozenProfileHandler` with `.profile.revision`,
  `.agent_id`, export paths and `.last_execution`.
- `skills_connected` and `knowledge_connected` are `False`: the factory
  preserves skills/knowledge in `snapshot.json` for traceability only. It does
  not claim the model used them.
- Later revisions cannot change an already-built handler. Build a new handler
  for a new revision.

## Conversation pinning and resume

`src/agent_manager/persistent_conversation.py` (`PersistentConversationHandler`)
pins a conversation to the profile revision and native Runner session stored in
`conversation_runtimes`:

1. reserve the model call (the repository pins the current revision on a new
   conversation);
2. return a cached successful result without launching again if the turn was
   already stored;
3. otherwise prepare/reuse the frozen export, resolve runtime defaults afresh,
   write a unique per-call execution-role document, and launch outside any
   database transaction;
4. require a non-empty returned session id, store reply + session + provenance,
   and release the reservation.

A pre-launch failure (bad defaults, bad pinned config) releases the
reservation for explicit retry. Everything at or after launch fails closed
into `blocked_reason` with no automatic replay. Resume state lives only in the
database and on disk; a fresh process reconstructs it.

## Platform operating instruction injection

The platform contract (`src/agent_manager/secretary_contract.py`,
`SECRETARY_CONTRACT`) is appended as an extra instruction document for the
platform secretary; it does not replace the customized identity prompt. In
`PersistentConversationHandler._prepare_execution`, when the execution role
exposes `create_task` (the secretary), the handler writes a
`knowledge_catalog.md` instruction containing `SECRETARY_CONTRACT` plus the
knowledge-base catalog. The per-call execution role is a copy of the pinned
base role with this file appended to `instructions`; the base export and the
stored profile are untouched.

## Skills

- `list_skills` reads the `/skills` directory inside the sandbox and each
  `SKILL.md` description. `install_skill` and `create_skill` write session
  records that are applied after the turn (see
  [Knowledge and integrations](knowledge-and-integrations.md)).
- A profile's `skills` array is a list of binding objects; the binding keys
  are arbitrary. `runner_config.skills` (names) and `AgentProfile.skills`
  (bindings) are both preserved and exported.
- Skills are not loaded by the freeze factory in this snapshot. Treat them as
  declared configuration, not as proven context.

## Pending interfaces

- T02I: scoped execution permissions and explicit per-instance paths/backend
  settings.
- T05: capability discovery injection and executable parity checks over the
  tool/profile surface.

## Related

- `src/agent_manager/storage/agents.py`
- `src/agent_manager/runtime_defaults.py`
- `src/agent_manager/profile_runtime.py`
- `src/agent_manager/persistent_conversation.py`
- `src/agent_manager/secretary_contract.py`
- `docs/runtime-defaults.md`
