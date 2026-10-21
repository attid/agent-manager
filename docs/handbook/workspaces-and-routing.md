# Workspaces and routing

`agent_id` and `workspace` are independent routing inputs. `agent_id` selects
the executor profile; `workspace` selects the host directory the sandbox may
write. `task_type` does **not** select an executor.

## Workspace resolution

`GET /workspaces/resolve` is a read-only preflight
(`src/kuzya/api/app.py`, `resolve_task_workspace`). It returns:

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

Rules (`src/kuzya/workspaces.py`):

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
| `inbox_dir` | Generated per-execution inbox view. It replaces the shared `<project>/inbox` scan and is mounted read-only at `/inbox`. Persistent conversations set it to the current turn's attachment view. |
| `attachments_dir` | Generated read-only conversation store mounted at `/attachments`; contains earlier same-conversation assets but never current ones. |
| `pi_command`, `runtime_paths` | Runner executable and dependency mounts. |
| `provider`, `model`, `pi_auth_provider`, `models_file` | Late-bound runtime selection (see [Profiles, runtime and skills](profiles-runtime-skills.md)). |

The Runner enforces the boundary
(`src/pi_runner/core.py`, `Config.load`): the project must be an existing
directory other than `/`, the real HOME and its parents are rejected,
`state_dir` must be outside the project, and dependency paths must not expose
system data, credential directories or the project itself.

`project_read_only=true` is a real filesystem boundary. Exposing `bash` does
not make a read-only project writable. When sandboxed agents run with restricted
network (`network: false` or `"restricted"`), host network access is isolated
via Bubblewrap network namespace (`--unshare-net`). A host-side forward proxy on a
Unix domain socket (`/session/egress_proxy.sock`) enforces domain allowlisting:
trusted LLM provider domains by default plus any explicit destinations declared in
`runner_config.allowed_hosts` (e.g. `eurmtl.me`). Requests to unauthorized destinations
receive HTTP 403 Forbidden.

## Per-task mount controls

Two execution keys in the **task metadata** override or extend the profile's
mount policy for a single task (the same merge pattern as `allowed_hosts`):

| Key | Type | Effect |
|---|---|---|
| `project_read_only` | `bool` | Mount **this task's** workspace `/workspace` read-only (`true`) or writable (`false`). An explicit task value wins over the profile default. |
| `readonly_paths` | `list[str]` | Extra existing host directories mounted read-only into the sandbox at the same absolute paths, merged on top of the profile's `readonly_paths`. |

Example — a task that works in its own project but may consult the kuzya
repository read-only:

```json
POST /tasks
{
  "title": "Переделать уведомления",
  "workspace": "mtl-task",
  "metadata": {
    "readonly_paths": ["/home/itolstov/Projects/mtl/kuzya"]
  }
}
```

The same keys can be set through `PATCH /tasks/{id}` before execution, so
API and MCP callers can flip read-only on or off per task. Every `readonly_paths`
entry must pass the Runner validation (an existing directory, outside the
project and state, no credential or system directories); an invalid path fails
the launch loudly. The effective values are written into the task's
`metadata.execution_context` and into the per-execution `workspace.md` the
agent sees, so both operators and the agent can tell which mounts are live.

## Execution context stored on tasks

When the worker runs a task it resolves the workspace again and records the
effective context in `task.metadata.execution_context`. Operators should read
`agent_id`, `workspace` and this metadata from the task rather than assume
what was requested.

## Instance isolation and operation scoping

- Explicit per-instance paths and backend settings are a first-class typed
  context (`src/kuzya/instance.py`, `InstanceContext`): state root,
  workspaces root, skills root, knowledge root, artifact root, default
  workspace and backend/OpenViking addresses. Components receive it from
  `RunnerConfig` instead of re-deriving a host path.
- `src/pi_runner/policy.mjs` writes `backend_api.json` only from the
  role's explicit backend document. There is no built-in address or token
  fallback; a filesystem-only role has no platform backend.
- `POST /operations` authorization checks the submitted `kind` and
  `target_id` (kind-specific action plus the `task:<id>` resource), so a
  review grant cannot submit a Git action and a task-scoped grant cannot
  target another task. Scoped callers cannot list the queue and read only
  their own operations (`src/kuzya/permissions.py`).

## Related

- `src/kuzya/workspaces.py`
- `src/kuzya/api/app.py` (`GET /workspaces/resolve`)
- `src/pi_runner/core.py`
- `docs/sandbox-security.md`
