# Runtime model defaults

`agent_manager.runtime_defaults` is the single host-side resolver for the
provider/model a secretary execution should use. The selection lives outside
the agent registry: the same logical agent can run on whatever model the host
currently offers, and changing that model must not require a database edit, a
new profile revision, a restart or a model call.

## Precedence

Fields are resolved independently, highest precedence first:

1. **Trusted role configuration** — explicit `provider`, `model`,
   `pi_auth_provider`, `models_file` values in the profile's `runner_config`
   (the mapping passed to `resolve`). These are copied, never mutated.
2. **Selected app TOML** — the `[default]` table of the caller-selected file.
3. **Host global Pi settings** — `defaultProvider` / `defaultModel` in
   `~/.pi/agent/settings.json` (`DEFAULT_GLOBAL_SETTINGS_PATH`).

A role configuration that already defines both `provider` and `model` is a
**complete override**: the default files are not read at all, so a fixed
profile keeps working even when those files are absent or malformed. When no
app file is configured, the global Pi default is the normal source.

Only the managed fields are read from a selected file. Unrelated Pi settings
are never exported or echoed, and unknown provider/model identifier strings
are preserved verbatim (no allowlist, no format requirement).

## Authentication reference

`pi_auth_provider` is an opaque reference handled by the existing Runner; this
module never reads, copies or validates credentials.

- If `pi_auth_provider` is configured explicitly (role config or app TOML), it
  is used as supplied.
- Otherwise it follows the resolved `provider`, so an inherited default tracks
  the provider that was actually selected.
- If a `model` is selected but neither a provider nor an explicit
  `pi_auth_provider` can be determined, resolution raises
  `RuntimeDefaultsConfigurationError`; the module never guesses or substitutes
  an unrelated model.

## Late binding

`RuntimeDefaultsResolver` stores only the configured paths. Every `resolve`
call re-reads the selected sources, so a manual edit takes effect for the next
execution with the same resolver object. Missing files are empty sources;
malformed TOML/JSON is a typed error when that source is consulted.

```python
from agent_manager.runtime_defaults import RuntimeDefaultsResolver

resolver = RuntimeDefaultsResolver(
    app_config_path="/etc/agent-manager/defaults.toml",  # optional
    global_settings_path=None,  # host ~/.pi/agent/settings.json
)
resolved = resolver.resolve(profile.runner_config)
print(resolved.provider, resolved.model, resolved.pi_auth_provider)
print(resolved.selection["model"].source)  # role_config | app_file | global_pi
```

`ResolvedRuntimeDefaults` exposes the copied `role_config`, the actual
`values`, per-field `selection` evidence and `to_provenance()` for inspection.
The caller's mapping (and therefore the stored registry policy) is never
modified.

### Manual `[default]` override

```toml
[default]
provider = "anthropic"
model = "claude-sonnet"
models_file = "/etc/agent-manager/custom-models.json"
# pi_auth_provider = "alias"   # optional explicit auth alias
```

### Global Pi source

`~/.pi/agent/settings.json`, read for `defaultProvider`/`defaultModel` only:

```json
{
  "defaultProvider": "anthropic",
  "defaultModel": "claude-sonnet"
}
```

The path is always interpreted against the **host** home and is injectable for
tests and explicit host use. The repository never installs under
`~/.agent-manager` or edits the user's real Pi settings.

## Frozen-profile integration

`freeze_profile_handler(..., runtime_defaults=resolver)` resolves the defaults
once for that frozen execution and merges the resolved values into the
exported `runner_role.json`. The export manifest (`snapshot.json`) keeps the
original profile snapshot and adds the actual selection provenance; the
adapter provenance record repeats the same evidence. Stored role policy is
unchanged. `runtime_defaults=None` (the default) preserves the previous
behaviour exactly and reads no host settings.

## Persistent-conversation per-call defaults

`PersistentConversationHandler` accepts a `RuntimeDefaultsResolver` (the
default is the host global Pi source with no app TOML; `defaults_config_path`
selects an optional app TOML). The conversation still pins the profile
revision, the base role export, the prompt, the project, the state directory
and the native session id, but it does **not** pin the transport. Every new
model call -- including a `resume` of an existing conversation -- resolves
the defaults once against the pinned base role policy and writes a unique
per-call execution-role JSON beside the base export. The base
`runner_role.json`, the profile snapshot and every earlier execution artifact
are never modified. Resume passes the execution role as `--agent` while
keeping the same state directory and native session. Each call's immutable
provenance records the execution-role path and the resolved values plus their
per-field sources.

A cached successful result returns before any defaults resolution and never
launches another child. A profile that fixes both `provider` and `model` is a
complete override and is unaffected by default-file edits; editing a default
file affects the next invocation of an inherited-default conversation.

## Safe pre-launch recovery

Late resolution means a malformed or missing defaults file can fail *after*
the reservation is taken but *before* the child adapter is invoked.
`ConversationRuntimeRepository.release_prelaunch_call` releases exactly that
reservation: it requires the current ownership token and the original turn
still running at the exact attempt, and it never sets the blocked latch. The
handler uses it only in the proven pre-launch window after the pinned paths
are attached, so an operator can repair the file, explicitly retry the failed
turn and run it without a duplicate model call. Once the child adapter is
entered, every failure stays fail-closed (blocked) with no automatic replay
or fresh-session fallback. The release is not a general unlock and is not
used for unknown or uncertain calls.

## Explicit non-goals

- No automatic blocked-session reconciliation and no failover policy.
- No credential lookup, copy, storage or secret-file access.
- No provider discovery, model download or network call.
- No provider/model allowlist and no schema change.
- No change to the Runner or the HTTP API. The persistent conversation
  handler integrates the resolver but adds no worker, dispatcher, Telegram,
  failover or uncertain-call replay.
