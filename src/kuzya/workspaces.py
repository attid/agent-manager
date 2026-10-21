"""Resolve host workspaces without silently replacing an explicit target."""

from collections.abc import Mapping
from pathlib import Path


def resolve_workspace(
    workspace: str | None,
    *,
    default_workspace: str | Path | None = None,
    workspace_map: Mapping[str, str] | None = None,
) -> Path:
    selected = (
        (workspace_map or {}).get(workspace, workspace)
        if workspace is not None
        else default_workspace
    )
    candidate = Path(selected).expanduser() if selected is not None else Path.cwd()
    if not candidate.is_dir():
        raise ValueError(
            f"Workspace does not exist or is not a directory: {workspace or candidate}"
        )
    resolved = candidate.resolve()
    # This is the same filesystem boundary enforced by Runner.Config.load.
    home = Path.home().resolve()
    if resolved == home or resolved in home.parents:
        raise ValueError(
            f"Workspace cannot expose the real HOME or its parent: {resolved}"
        )
    return resolved
