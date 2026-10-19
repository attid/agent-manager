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

`attach_document` is the exception to the plain receipt rule: its record
belongs to one explicit tool invocation and one conversation turn. The Runner
writes the current turn identity to `/session/turn.json` for each launch, and
the policy stores it together with the `toolCallId` in `attachments.json`.
`PersistentConversationHandler` drains only records whose `turn_id` matches
the current turn, appends the `[SEND_FILE: <target>]` marker, and acknowledges
them with `pending_items`/`acknowledge_items` only after the reply is durable.
A resumed unrelated turn therefore cannot replay an earlier request; a retry
of the same invocation is stable; and a later deliberate request for the same
target still sends again. Legacy records without a `turn_id` are preserved as
audit history and never replayed.

Drain targets:

| Session file | Applied as |
|---|---|
| `written_knowledge.json` | File under the knowledge root; paths escaping the root are rejected; writes into `agents/` are ignored. |
| `saved_projects.json` | `projects/<name>/summary.md`. |
| `install_skills.json` | Copy a directory into the skills root. |
| `created_skills.json` | Create `<skills>/<name>/SKILL.md` and scripts. |
| `archived_agents.json` | Archive a non-core agent. |
| `created_agents.json` | Legacy compatibility; never overwrites an existing profile. |
| `attachments.json` | Append `[SEND_FILE: <target>]` to the reply; only records tagged with the current turn identity are drained, and acknowledgement follows reply persistence. |
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
  API, and both return a specific "OpenViking не настроен" diagnostic when the
  instance does not inject `openviking_url`/`openviking_token`. They never
  invent an address or credential: `getBackendConfig()` returns only the
  explicit `/session/backend_api.json` document written from the per-instance
  role configuration.

## Files and documents

- `attach_document(target)` writes a session record tagged with the
  invocation and turn identity; the reply handler appends
  `[SEND_FILE: <target>]` for that turn only, which the Telegram layer
  interprets to send the file. Legacy records without a turn identity remain
  audit history and are never replayed.
- Inbound Telegram media is owned by the Telegram media directory and then
  staged once, by `PersistentConversationHandler`, into a server-owned
  conversation/turn store under `<artifact_root>/attachments/`. Directory
  keys are hashes of server-side ids; raw inbound basenames and paths are
  never used for storage.
- The child Runner receives only the current turn's files as
  `/workspace/inbox` (`runner_config.inbox_dir`). The profile's shared
  `project/inbox` is never scanned for a persistent conversation, so a new or
  reset conversation cannot inherit legacy files. Earlier attachments from
  the same conversation are mounted read-only at `/attachments`
  (`runner_config.attachments_dir`) and are labelled as prior, not current.
- The turn prompt carries a generated `attachment_context`: an explicit
  `Current message attachments: none` for text-only messages, current
  attachment paths with hashes, and prior same-conversation assets. Prompt
  and metadata agree, and retry of the same turn reuses the canonical staged
  bytes even if the Telegram source file is gone.

## Telegram

- Bot and handlers live in `src/agent_manager/telegram/`.
- Voice messages are transcribed and delivered with a prefix that marks the
  text as an automatic speech transcript with possible ASR errors; agents must
  treat the transcript as the user's message and must not claim the audio did
  not exist.
- `TelegramNotifier` posts task-finished notifications when configured.
- `cmd_analyze` shows a stored analysis report without a model call, and
  otherwise submits the real durable operation and renders its stored
  pending/failed/succeeded outcome. A repeated view replays the same
  revision-bound operation; `/analyze <task_id> refresh` starts a new one; see
  [Analysis and recommendations](analysis-and-recommendations.md).
- `KnowledgeBase.initialize` copies `docs/handbook/*.md` into
  `<knowledge_root>/handbook` (content-idempotent) so the read-only platform
  handbook is mounted at `/knowledge/handbook` for agent discovery.

## Infrastructure topology

Kustomize, Helm and Terraform files are not read by the running platform in
this snapshot. Any infrastructure material under `scripts/` or `docs/` is
deployment tooling, not part of the application runtime contract.

## Discovery and integrations

- Secretary and `system-*` agents receive the handbook discovery injection on
  every run/resume; the executable list is filtered by the profile's tools
  while the full handbook index stays discoverable.
- Analysis reports and recommendations are stored and retrievable; approval
  and apply follow the rules in
  [Approvals and revisions](approvals-and-revisions.md) and
  [Analysis and recommendations](analysis-and-recommendations.md).
- Skills, knowledge and infrastructure remain declarative unless an execution
  path actually connects them; the capability inventory marks local effects
  explicitly rather than claiming they are loaded.

## Related

- `src/agent_manager/knowledge.py`
- `src/agent_manager/session_operations.py`
- `src/agent_manager/persistent_conversation.py`
- `src/pi_runner/policy.mjs`
- `docs/knowledge-base.md`, `docs/telegram.md`
