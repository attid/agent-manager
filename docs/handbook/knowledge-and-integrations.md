# Knowledge and integrations

## Knowledge base

`KnowledgeBase` (`src/kuzya/knowledge.py`) manages a filesystem tree
rooted at `KUZYA_KNOWLEDGE_ROOT` (default
`~/.kuzya/data/knowledge`):

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

### Deleting one document

`DELETE /knowledge/{path}` removes exactly one existing Markdown document.
The secretary tool `delete_knowledge(path)` calls that route, so the same
server-side rules apply to a tool call and to raw HTTP:

- the path is relative to the knowledge root and must end in `.md`;
- absolute paths, empty/`.`/`..` segments, backslashes, wildcard characters
  and directory names are rejected;
- any symlink component is rejected, and the resolved target must stay inside
  the knowledge root;
- `agents/` is protected because profiles are managed by the profile API;
- a missing file returns `404`; invalid or protected paths return `400`;
- the exact file is unlinked, then `git_commit` records the removal when the
  knowledge tree is a git repository.

There is no wildcard or recursive deletion. Deleting
`инструкции/секретарь.md` never touches `instructions/secretary.md`. The
policy requires an explicit user confirmation of the exact path before the
tool is called; this is a dialog rule in the tool description and
`SECRETARY_CONTRACT`, not a separate server-attested approval token. The tool
is exposed only when it appears in the profile's `tools` list and its
`knowledge.write` action is granted to that execution. A new default secretary
profile includes it; bootstrap does not silently add it to an existing
customized tool list, so such a profile must be updated explicitly through the
profile API.

### Deferred write acknowledgement

`src/kuzya/session_operations.py` (`unapplied_items`) yields each
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
| `clarification.json` | Signals `waiting_for_input`. |
| `actions.json` | Legacy `trigger_*` records; submitted as durable operations. |

## Skills

- Installed skills live in `~/.kuzya/data/skills/<name>/`
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
- Both are direct HTTP calls from `policy.mjs`, not through the Kuzya
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
  `/inbox` (`runner_config.inbox_dir`). The profile's shared
  `project/inbox` is never scanned for a persistent conversation, so a new or
  reset conversation cannot inherit legacy files. Earlier attachments from
  the same conversation are mounted read-only at `/attachments`
  (`runner_config.attachments_dir`) and are labelled as prior, not current.
- The turn prompt carries a generated `attachment_context`: an explicit
  `Current message attachments: none` for text-only messages, current
  attachment paths with hashes, and prior same-conversation assets. Prompt
  and metadata agree, and retry of the same turn reuses the canonical staged
  bytes even if the Telegram source file is gone.
- Task attachments (declared on `TaskCreate` / `TaskPatch` or uploaded via `POST /tasks/{task_id}/attachments`) are stored in the task's artifact directory and staged read-only into `/inbox` (chmod `0o444`) for the executing task worker.

## Telegram

- Bot and handlers live in `src/kuzya/telegram/`.
- Voice messages are transcribed and delivered with a prefix that marks the
  text as an automatic speech transcript with possible ASR errors; agents must
  treat the transcript as the user's message and must not claim the audio did
  not exist.
- `TelegramNotifier` posts task-finished notifications when configured.
- Direct agent conversations are started from `/talk` → «👥 Выбрать агента».
  Only profiles with `runner_config.talkable: true` are listed. The chosen
  conversation carries `metadata.talkable_agent`/`talk_active`, keeps its
  native Runner session, and appears as «🗣 Разговоры: N» in the task
  category keyboard. Both the synchronous worker and the durable outbox
  select the agent keyboard (`🏁 Закончить`/`💬 С секретарём`) from the same
  conversation metadata. Ending it releases the agent workspace; see
  [Task lifecycle](task-lifecycle.md#direct-conversations-with-task-agents).
- `cmd_analyze` shows a stored analysis report without a model call, and
  otherwise submits the real durable operation and renders its stored
  pending/failed/succeeded outcome. A repeated view replays the same
  revision-bound operation; `/analyze <task_id> refresh` starts a new one; see
  [Analysis and recommendations](analysis-and-recommendations.md).
- `KnowledgeBase.initialize` copies `docs/handbook/*.md` into
  `<knowledge_root>/handbook` (content-idempotent) so the read-only platform
  handbook is mounted at `/knowledge/handbook` for agent discovery.

## Dialog context, UUID and Telegram delivery

Every secretary-related message that reaches the owner's Telegram chat is
rendered by the unified messenger (`src/kuzya/telegram/messenger.py`)
and ends with the full conversation UUID hashtag (for example
`#34acbd180d064f6d8588a039c0f1b6bc`) on every part and attachment caption.
The hashtag is for reading, copying and diagnostics only; routing never parses
it back out of the visible text.

- **API mirroring.** A user question appended through the API (`/ask`,
  `/conversations/ask` or `POST .../messages` with the default source) is
  mirrored into the owner's chat as «📨 Вопрос секретарю через API» with the
  server-verified caller (free-form request metadata is never trusted as an
  identity), followed by the secretary reply — question strictly before
  answer. A failed turn is mirrored as «⚠️ Ошибка исполнения». Telegram-origin
  questions are already in the chat and are never copied.
- **Durable outbox.** Delivery rows (`telegram_deliveries`, migration `0012`)
  are written transactionally with the messages they mirror and processed by
  an at-least-once sender: an atomic `pending → sending` claim, per-part
  progress (`parts_sent`), startup requeue of stale `sending` rows. A network
  failure with an unknown outcome is logged as such and retried; a definite
  Telegram API rejection marks the row `failed`. Exactly-once delivery is not
  promised: a part accepted by Telegram just before a crash may arrive twice.
- **Reply routing.** `telegram_message_links` binds each Telegram chat/message
  pair to its conversation (and task, when bound); user input registers links
  transactionally with the message insert, bot output registers them right
  after each sent part. An incoming Reply resolves its target through
  `GET /telegram/links/{chat_id}/{telegram_message_id}` **before** any
  active-mode or task-clarification logic, so a reply into dialog A can never
  resume an unrelated waiting task B and never switches the active context.
  A reply to a bot message whose link is lost (dialog deleted or sent before
  this table existed) gets an explanation instead of silent rerouting; a reply
  to an unlinked human message is ordinary input.
- **Context switching.** Every dialog message carries a button: «▶️ Продолжить
  задачу №N» for task-bound dialogs (`switch_task:`) or «💬 Продолжить этот
  диалог» (`switch_conversation:`). The press verifies the target exists,
  switches the active context and confirms it with the full UUID; a repeated
  press is safe. Messages about another dialog show a snapshot plate
  («↩️ Другой диалог · сейчас активна задача №41») taken at send time.

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

- `src/kuzya/knowledge.py`
- `src/kuzya/session_operations.py`
- `src/kuzya/persistent_conversation.py`
- `src/pi_runner/policy.mjs`
- `docs/knowledge-base.md`, `docs/telegram.md`
