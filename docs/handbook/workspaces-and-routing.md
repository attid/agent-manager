# Workspaces and routing

`agent_id` and `workspace` are independent routing inputs. `agent_id` selects
the executor profile; `workspace` selects the host directory the sandbox may
write. `task_type` does **not** select an executor.

## Workspace resolution

`GET /workspaces/resolve` is a read-only preflight
(`src/agent_manager/api/app.py`, `resolve_task_workspace`). It returns:

```json
{
  "host_path": "/absolute/host/directory",
  "sandbox_path": "/workspace",
  "agent_id": "selected-agent",
  "profile_revision": 3,
  "project_read_only": false,
  "network": true
}
```

Rules (`src/agent_manager/workspaces.py`):

- An explicit `workspace` that does not exist is an error (`422`), never a
  silent fallback to another project.
- When `workspace` is omitted, the selected profile's
  `runner_config.project` is used; the API's `default_workspace` is the final
  fallback.
- A `workspace_map` may translate an alias to a host path. Aliases are not
  registered automatically.
- The real HOME and its parents are rejected (`ValueError` → `422`).
- The project is always mounted at `/workspace` inside the sandbox. A host
  `/home/user/Projects` is `/workspace` inside, not `/workspace/Projects`.
- An archived profile (`runner_config.archived` or `role == "archived"`)
  returns `409`.
- A missing profile returns `404`.

Both `create_task` and `update_task` call this preflight before writing and
copy the resulting `host_path` into `tasks.workspace`, so a task cannot be
routed to an unverified directory through those tools.

## Access policy per profile

An `AgentProfile.runner_config` carries the execution policy:

| Key | Meaning |
|---|---|
| `project` | Default host project directory. |
| `workdir` | Optional working directory inside the project. |
| `project_read_only` | Filesystem read-only boundary for the target project. |
| `network` | Enables or disables all sandbox networking, including direct model API access. |
| `readonly_paths` | Additional read-only mounts. |
| `instructions` | Extra instruction files handed to the runner. |
| `tools` | Tool names exposed to the model for this profile. |
| `skills` | Skill names bound to the profile. |
| `state_dir` | Absolute Runner state directory; must be outside the project. |
| `pi_command`, `runtime_paths` | Runner executable and dependency mounts. |
| `provider`, `model`, `pi_auth_provider`, `models_file` | Late-bound runtime selection (see [Profiles, runtime and skills](profiles-runtime-skills.md)). |

The Runner enforces the boundary
(`src/pi_runner/core.py`, `Config.load`): the project must be an existing
directory other than `/`, the real HOME and its parents are rejected,
`state_dir` must be outside the project, and dependency paths must not expose
system data, credential directories or the project itself.

`project_read_only=true` is a real filesystem boundary. Exposing `bash` does
not make a read-only project writable. `network=false` disables sandbox
networking entirely, including direct model API access; there is no
"model on, tools off" mode.

## Execution context stored on tasks

When the worker runs a task it resolves the workspace again and records the
effective context in `task.metadata.execution_context`. Operators should read
`agent_id`, `workspace` and this metadata from the task rather than assume
what was requested.

## Known gaps in this snapshot

These are real gaps the later integration tasks own; do not rely on the
current behaviour:

- The Runner composition root passes `default_workspace` into workers and the
  API, but per-instance explicit paths/backend settings are not yet a
  first-class typed context (T02I / T05 contract).
- `src/pi_runner/policy.mjs` falls back to built-in backend addresses when
  `/session/backend_api.json` is absent. Execution instances should always
  inject explicit backend settings; the hardcoded fallback must not be used
  for production executions (T02I / T05 contract).
- Operation authorization scoped to kind and target is not implemented yet
  (T02I): `POST /operations` currently accepts any known kind from any
  authorized caller.

## Related

- `src/agent_manager/workspaces.py`
- `src/agent_manager/api/app.py` (`GET /workspaces/resolve`)
- `src/pi_runner/core.py`
- `docs/sandbox-security.md`
