# Knowledge and integrations

## Knowledge base

`KnowledgeBase` (`src/agent_manager/knowledge.py`) manages a filesystem tree
rooted at `AGENT_MANAGER_KNOWLEDGE_ROOT` (default
`~/.local/share/agent-manager/knowledge`):

```
<root>/
├── projects/<project>/summary.md
├── components/<component>/rules.md
├── agents/<agent_id>.md          # mirror of the current profile revision
└── docs/...                      # free-form documents (e.g. docs/drafts)
```

The tree may be a git repository; writes are committed by `git_commit` when a
`.git` directory exists.

### Reading

- `read_knowledge(query)` resolves a direct path, a path relative to the root,
  `agents/<name>.md`, `projects/<name>/summary.md`, or
  `components/<name>/rules.md`.
- `list_knowledge(category?)` walks the tree and returns `.md` paths.
- `KnowledgeBase.get_catalog_index()` produces a compact catalog that is
  injected into the secretary execution role as an instruction file.

### Writing

Two kinds of write exist:

1. **Synchronous API writes** — `POST /agents`, `PATCH /agents/{id}`,
   `DELETE /agents/{id}` write the profile and call `save_agent_profile` /
   archive, so `agents/<id>.md` mirrors the database.
2. **Session-deferred writes** — `write_knowledge`, `save_project_summary`,
   `update_component_rules`, `archive_agent`, `install_skill`, `create_skill`,
   `attach_document`, `ask_clarification` append a record to a JSON file in
   the session directory (`/session/<name>.json`). After the turn completes,
   `PersistentConversationHandler` drains those files.

### Deferred write acknowledgement

`src/agent_manager/session_operations.py` (`unapplied_items`) yields each
record once and records a SHA-256 receipt in `<name>.applied.json` after the
consumer body returns. A crash before the receipt means the record is retried,
so consumers must be idempotent. This is why a deferred tool reports
`pending`: the effect has not been applied yet. Do not repeat the tool call to
"check" it.

Drain targets:

| Session file | Applied as |
|---|---|
| `written_knowledge.json` | File under the knowledge root; paths escaping the root are rejected; writes into `agents/` are ignored. |
| `saved_projects.json` | `projects/<name>/summary.md`. |
| `install_skills.json` | Copy a directory into the skills root. |
| `created_skills.json` | Create `<skills>/<name>/SKILL.md` and scripts. |
| `archived_agents.json` | Archive a non-core agent. |
| `created_agents.json` | Legacy compatibility; never overwrites an existing profile. |
| `attachments.json` | Append `[SEND_FILE: <target>]` to the reply. |
| `clarification.json` | Signals `waiting_user_input`. |
| `actions.json` | Legacy `trigger_*` records; submitted as durable operations. |

## Skills

- Installed skills live in `~/.local/share/agent-manager/skills/<name>/`
  (mirrored inside the sandbox at `/skills`).
- `list_skills` reads `SKILL.md` descriptions.
- `install_skill` and `create_skill` are session-deferred writes.
- A profile binds skills through `runner_config.skills` (names) and
  `AgentProfile.skills` (binding objects). In this snapshot the execution
  factory exports skills for traceability but does not load them; do not claim
  the model used a skill.

## OpenViking

- `openviking_search(query, limit?)` calls
  `POST <openviking_url>/api/v1/search/search`.
- `openviking_read(uri)` calls
  `GET <openviking_url>/api/v1/content/read?uri=...`.
- Both are direct HTTP calls from `policy.mjs`, not through the Agent Manager
  API, and both return an error string on failure rather than a structured
  status.

**Known gap (T02I / T05 contract):** when `/session/backend_api.json` is
absent, `getBackendConfig()` falls back to built-in backend/OpenViking
addresses and an embedded token. Execution instances must inject explicit
backend settings and tokens; the built-in fallback must not be relied on and
the embedded credential must be externalized.

## Files and documents

- `attach_document(target)` writes a session record; the reply handler appends
  `[SEND_FILE: <target>]`, which the Telegram layer interprets to send the
  file.
- Inbound media paths are copied into `<project>/inbox/` before the child run
  (`PersistentConversationHandler._execute`).

## Telegram

- Bot and handlers live in `src/agent_manager/telegram/`.
- Voice messages are transcribed and delivered with a prefix that marks the
  text as an automatic speech transcript with possible ASR errors; agents must
  treat the transcript as the user's message and must not claim the audio did
  not exist.
- `TelegramNotifier` posts task-finished notifications when configured.
- `cmd_analyze` currently prints hardcoded findings; see
  [Analysis and recommendations](analysis-and-recommendations.md).

## Infrastructure topology

Kustomize, Helm and Terraform files are not read by the running platform in
this snapshot. Any infrastructure material under `scripts/` or `docs/` is
deployment tooling, not part of the application runtime contract.

## Pending interfaces

- T05: capability discovery injection for secretary and system agents; skills
  and knowledge connection (if implemented) must be explicit and truthful.
- T04: evidence/recommendation storage and API.

## Related

- `src/agent_manager/knowledge.py`
- `src/agent_manager/session_operations.py`
- `src/agent_manager/persistent_conversation.py`
- `src/pi_runner/policy.mjs`
- `docs/knowledge-base.md`, `docs/telegram.md`
