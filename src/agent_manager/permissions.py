"""Execution-scoped API actions and resources.

The platform exposes an HTTP backend to child Pi executions. Administrative
user credentials must never be handed to a developer, reviewer or analyzer
sandbox, and a restricted execution must not become unrestricted by reading
its credential file and calling the backend directly with ``curl``.

This module is the reusable authorization seam:

* :class:`ExecutionPermissions` issues opaque, unpredictable tokens and keeps
  the granted actions and resources in memory for the lifetime of the
  server process.
* Action names are derived from the profile's configured tools, never from a
  role or name allowlist (see :func:`actions_for_tools`).
* Resources identify the concrete objects an execution may touch, for
  example ``task:<id>`` or ``agent:<id>``. The wildcard ``*`` is reserved for
  administrative principals.
* :func:`resolve_requirement` maps an HTTP method and path to the action and
  resource an endpoint needs, so middleware enforces the same rule for every
  client, including a raw HTTP call from a shell.

T01/T05 seam
------------

The operation endpoints and trigger dispatch added by T01/T05 must register
their routes in :data:`_ROUTE_RULES` and call
:meth:`ExecutionPermissions.authorize` (or use the middleware, which already
does) with the action and ``operation:<id>``/``task:<id>`` resource. The
middleware stores the resolved grant on ``scope['state']`` as
``execution_grant`` so a handler can read the caller's exact scope through
``request.state.execution_grant`` without re-parsing the token.
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "ACTIONS_FOR_TOOLS",
    "ADMIN_RESOURCE",
    "ExecutionGrant",
    "ExecutionPermissions",
    "Requirement",
    "actions_for_tools",
    "resolve_requirement",
]


#: Resource value granted to administrative principals only.
ADMIN_RESOURCE = "*"

# Logical actions used by the HTTP backend.
WORKSPACES_READ = "workspaces.read"
TASKS_READ = "tasks.read"
TASKS_WRITE = "tasks.write"
CONVERSATIONS_READ = "conversations.read"
CONVERSATIONS_WRITE = "conversations.write"
TURNS_READ = "turns.read"
TURNS_WRITE = "turns.write"
AGENTS_READ = "agents.read"
AGENTS_WRITE = "agents.write"
KNOWLEDGE_READ = "knowledge.read"
KNOWLEDGE_WRITE = "knowledge.write"
SKILLS_READ = "skills.read"
SKILLS_WRITE = "skills.write"
REVIEW_TRIGGER = "review.trigger"
ANALYSIS_TRIGGER = "analysis.trigger"
GIT_ACTIONS = "git.actions"


#: Tool name -> backend action. Tools absent from this mapping are local
#: (filesystem or session-file) tools and grant no backend action. Knowledge,
#: skill, clarification, attachment, OpenViking and trigger tools all operate
#: on local files or on the parent process and therefore grant no HTTP
#: authority here. T01/T05 can add an action for a new HTTP-backed tool.
ACTIONS_FOR_TOOLS: Mapping[str, str] = {
    "list_tasks": TASKS_READ,
    "get_task": TASKS_READ,
    "get_task_logs": TASKS_READ,
    "create_task": TASKS_WRITE,
    "update_task": TASKS_WRITE,
    "resume_task_session": TASKS_WRITE,
    "resolve_workspace": WORKSPACES_READ,
    "list_agents": AGENTS_READ,
    "get_agent": AGENTS_READ,
    "create_agent": AGENTS_WRITE,
    "update_agent": AGENTS_WRITE,
    "archive_agent": AGENTS_WRITE,
}

#: Actions that require broad (wildcard-resource) authority because they
#: create, route or mutate platform objects rather than a single task.
MANAGEMENT_ACTIONS: frozenset[str] = frozenset(
    {
        TASKS_WRITE,
        AGENTS_WRITE,
        KNOWLEDGE_WRITE,
        SKILLS_WRITE,
        GIT_ACTIONS,
        REVIEW_TRIGGER,
        ANALYSIS_TRIGGER,
    }
)


def resources_for_execution(
    actions: Iterable[str],
    *,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> frozenset[str]:
    """Return the resource scope for an execution with ``actions``.

    Management actions need wildcard authority. Everything else is scoped to
    the exact task and agent of the execution, so a raw HTTP call from the
    sandbox cannot read or mutate an unrelated task or agent.
    """
    action_set = frozenset(actions)
    if action_set & MANAGEMENT_ACTIONS:
        return frozenset({ADMIN_RESOURCE})
    resources: set[str] = set()
    if task_id:
        resources.add(f"task:{task_id}")
    if agent_id:
        resources.add(f"agent:{agent_id}")
    if not resources:
        # A purely read-scoped execution with no identity cannot safely be
        # constrained; refuse to issue an implied broad grant by scoping to
        # nothing. The middleware then denies every declared route.
        return frozenset()
    return frozenset(resources)


def actions_for_tools(tools: Iterable[str] | None) -> frozenset[str]:
    """Return the set of backend actions granted by ``tools``.

    Unknown tools are ignored: a profile cannot widen its own grant by naming
    an arbitrary tool. The result is derived only from configured tools, not
    from the agent's role or name.
    """
    return frozenset(
        ACTIONS_FOR_TOOLS[name]
        for name in (tools or ())
        if name in ACTIONS_FOR_TOOLS
    )


@dataclasses.dataclass(frozen=True)
class ExecutionGrant:
    """One execution-scoped credential and its exact authority."""

    token: str
    agent_id: str
    actions: frozenset[str]
    resources: frozenset[str]
    created_at: float
    label: str | None = None

    @property
    def is_admin(self) -> bool:
        """Administrative grants are never issued by this module."""
        return ADMIN_RESOURCE in self.resources and ADMIN_RESOURCE in self.actions

    def to_document(self) -> dict[str, Any]:
        """Return a credential-free description for logs and tests."""
        return {
            "agent_id": self.agent_id,
            "actions": sorted(self.actions),
            "resources": sorted(self.resources),
            "label": self.label,
        }


@dataclasses.dataclass(frozen=True)
class Requirement:
    """Action and optional resource an HTTP route needs."""

    action: str
    resource: str | None = None
    resource_kind: str | None = None


# Method, path pattern, action, resource kind. ``None`` resource kind means a
# collection route that a scoped execution may not use.
_ROUTE_RULES: tuple[tuple[str, str, str, str | None], ...] = (
    ("GET", "/workspaces/resolve", WORKSPACES_READ, None),
    ("POST", "/tasks", TASKS_WRITE, None),
    ("POST", "/imports/tasks", TASKS_WRITE, None),
    ("GET", "/tasks", TASKS_READ, None),
    ("GET", "/tasks/{task_id}", TASKS_READ, "task"),
    ("PATCH", "/tasks/{task_id}", TASKS_WRITE, "task"),
    ("POST", "/conversations", CONVERSATIONS_WRITE, None),
    ("GET", "/conversations", CONVERSATIONS_READ, None),
    ("GET", "/conversations/{conversation_id}", CONVERSATIONS_READ, "conversation"),
    (
        "POST",
        "/conversations/{conversation_id}/messages",
        CONVERSATIONS_WRITE,
        "conversation",
    ),
    (
        "GET",
        "/conversations/{conversation_id}/messages",
        CONVERSATIONS_READ,
        "conversation",
    ),
    (
        "POST",
        "/conversations/{conversation_id}/turns",
        TURNS_WRITE,
        "conversation",
    ),
    ("GET", "/turns/{turn_id}", TURNS_READ, "turn"),
    ("POST", "/agents", AGENTS_WRITE, None),
    ("GET", "/agents", AGENTS_READ, None),
    ("GET", "/agents/{agent_id}", AGENTS_READ, "agent"),
    ("GET", "/agents/{agent_id}/revisions", AGENTS_READ, "agent"),
    ("GET", "/agents/{agent_id}/revisions/{revision}", AGENTS_READ, "agent"),
    ("PATCH", "/agents/{agent_id}", AGENTS_WRITE, "agent"),
    ("DELETE", "/agents/{agent_id}", AGENTS_WRITE, "agent"),
)


def _match(pattern: str, path: str) -> str | None:
    """Return the captured value for a single-parameter pattern, else None.

    Only one ``{name}`` parameter is used per resource rule; the captured
    value is the whole remaining path segment. Patterns without a parameter
    must match the path exactly.
    """
    pattern_parts = pattern.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    captures: list[str] = []
    if len(pattern_parts) != len(path_parts):
        return None
    for expected, actual in zip(pattern_parts, path_parts):
        if expected.startswith("{") and expected.endswith("}"):
            captures.append(actual)
        elif expected != actual:
            return None
    return captures[0] if captures else ""


def resolve_requirement(method: str, path: str) -> Requirement | None:
    """Map an HTTP request to its required action and resource.

    Returns ``None`` for paths that are public or unknown. ``/health`` is
    public by design. An unknown route is treated as ``None`` here because
    the router itself returns ``404`` before any handler runs; authenticated
    execution tokens still need a matching grant for every declared route.
    """
    method = method.upper()
    if path == "/health":
        return None
    path = path.rstrip("/") or "/"
    for rule_method, pattern, action, resource_kind in _ROUTE_RULES:
        if rule_method != method:
            continue
        captured = _match(pattern, path)
        if captured is None:
            continue
        resource = (
            f"{resource_kind}:{captured}" if resource_kind is not None else None
        )
        return Requirement(
            action=action,
            resource=resource,
            resource_kind=resource_kind,
        )
    return None


class ExecutionPermissions:
    """Thread-safe in-memory registry of execution-scoped grants.

    Grants live for the lifetime of the server process. An execution and its
    server share that lifetime, so persistence is deliberately not required
    here; T01's durable operations can re-issue a grant when they resume an
    execution. Revocation supports explicit cleanup and tests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._grants: dict[str, ExecutionGrant] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._grants)

    @property
    def enabled(self) -> bool:
        """Whether any scoped grant has been issued."""
        return len(self) > 0

    def issue(
        self,
        *,
        agent_id: str,
        actions: Iterable[str],
        resources: Iterable[str],
        label: str | None = None,
        token: str | None = None,
    ) -> ExecutionGrant:
        """Issue a new unpredictable token for one execution."""
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent_id must be a non-empty string")
        grant = ExecutionGrant(
            token=token or secrets.token_urlsafe(32),
            agent_id=agent_id,
            actions=frozenset(actions),
            resources=frozenset(resources),
            created_at=time.time(),
            label=label,
        )
        if grant.token in ("", ADMIN_RESOURCE):
            raise ValueError("execution tokens must be non-empty non-wildcard strings")
        with self._lock:
            if grant.token in self._grants:
                raise ValueError("execution token already exists")
            self._grants[grant.token] = grant
        return grant

    def revoke(self, token: str) -> bool:
        """Remove one grant; returns whether it existed."""
        with self._lock:
            return self._grants.pop(token, None) is not None

    def resolve(self, token: str) -> ExecutionGrant | None:
        """Return the grant for ``token``, or ``None``."""
        if not token:
            return None
        with self._lock:
            return self._grants.get(token)

    def authorize(
        self,
        token: str,
        action: str,
        resource: str | None = None,
    ) -> bool:
        """Return whether ``token`` may perform ``action`` on ``resource``.

        A collection route (``resource is None``) requires the wildcard
        resource, so a task-scoped execution can never enumerate every task.
        A concrete resource requires either that exact resource or the
        wildcard.
        """
        grant = self.resolve(token)
        if grant is None:
            return False
        if action not in grant.actions and ADMIN_RESOURCE not in grant.actions:
            return False
        if resource is None:
            return ADMIN_RESOURCE in grant.resources
        return (
            ADMIN_RESOURCE in grant.resources or resource in grant.resources
        )
