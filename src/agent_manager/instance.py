"""Typed per-instance paths and backend settings.

One Agent Manager installation owns one instance context. Every component
that writes state, resolves workspaces or connects a child execution to the
platform backend receives the same explicit :class:`InstanceContext` instead
of reaching for the process ``HOME`` directory or a compiled-in production
address.

The context is built once from :class:`~agent_manager.runner.RunnerConfig`
and then passed into the knowledge bootstrap, the task worker, the
persistent conversation handler and the API application. Nothing in the
runtime re-derives these values from the environment or from a default host
location.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

__all__ = ["InstanceContext"]


@dataclasses.dataclass(frozen=True)
class InstanceContext:
    """Explicit, immutable configuration for a single isolated instance.

    Attributes:
        state_root: Parent directory for per-agent Runner state directories.
        workspaces_root: Parent directory for per-agent default workspaces.
        skills_root: Parent directory for installed skills.
        knowledge_root: Knowledge base root directory.
        artifact_root: Parent directory for exported artifacts and run logs.
        default_workspace: Optional default project workspace.
        backend_url: Platform HTTP backend used by child executions. An empty
            string means "no platform backend"; child tools must not invent a
            fallback address.
        openviking_url: Optional OpenViking address. Empty disables the
            OpenViking tools with a truthful diagnostic.
        openviking_token: Optional OpenViking credential. Never logged.
    """

    state_root: Path
    workspaces_root: Path
    skills_root: Path
    knowledge_root: Path
    artifact_root: Path
    default_workspace: Path | None = None
    backend_url: str = ""
    openviking_url: str = ""
    openviking_token: str = ""

    def child_backend(self, api_token: str) -> dict[str, str]:
        """Return the child-facing backend configuration mapping.

        ``api_token`` is an execution-scoped credential, never an
        administrative token. Empty optional addresses are omitted so the
        child reports them as unconfigured instead of guessing.
        """
        backend: dict[str, str] = {"backend_url": self.backend_url}
        if api_token:
            backend["api_token"] = api_token
        if self.openviking_url:
            backend["openviking_url"] = self.openviking_url
        if self.openviking_token:
            backend["openviking_token"] = self.openviking_token
        return backend
