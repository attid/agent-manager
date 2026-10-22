"""Atomic workspace coordination for tasks and direct-agent conversations.

A task and a direct conversation must never run two Runner children in the
same workspace at once. Tasks already serialize through
:meth:`kuzya.storage.TaskRepository.claim_in_progress`; this module supplies
the matching conversation side and the shared predicates both sides use.

Coordination uses the existing durable rows, not a second lease table:

* a task occupies a workspace while it is ``in_progress`` with
  ``metadata.execution_status == "running"`` or while it is
  ``ready_to_commit``;
* a conversation occupies a workspace while ``metadata.talk_active`` is true.

Every mutation runs inside ``BEGIN IMMEDIATE``. SQLite therefore serializes a
task claim and a conversation start/end, so both observe the same committed
state. A conversation that finds its workspace busy is not activated: its
turn stays pending (the dispatcher defers it) and a later attempt starts it
after the task finishes. Ending a conversation first marks it ``closing`` so
new turns are refused, then releases ``talk_active`` only when no pending or
running turn remains.

The native Runner session is never touched here; ending a conversation keeps
the conversation runtime binding so a later selection resumes it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .database import Database
from .models import AgentProfile, Conversation, Task, Turn, utcnow

__all__ = [
    "ConversationTalkClosedError",
    "talk_metadata_is_closing",
    "ConversationTalkNotFoundError",
    "WorkspaceBusyError",
    "WorkspaceTalkRepository",
    "active_talk_locks",
    "resolve_effective_execution",
    "task_waits_for_talk",
]


class WorkspaceTalkError(Exception):
    """Base class for direct-talk workspace coordination failures."""


class WorkspaceBusyError(WorkspaceTalkError):
    """Raised when a task or another conversation owns the workspace."""

    def __init__(self, conversation_id: str, workspace: str | None) -> None:
        super().__init__(
            f"workspace {workspace!r} is busy; conversation "
            f"{conversation_id} must wait"
        )
        self.conversation_id = conversation_id
        self.workspace = workspace


class ConversationTalkClosedError(WorkspaceTalkError):
    """Raised when a turn is submitted to a closing or ended talk."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(
            f"conversation {conversation_id} is not accepting agent turns"
        )
        self.conversation_id = conversation_id


class ConversationTalkNotFoundError(WorkspaceTalkError):
    """Raised when a talk operation targets a missing/non-talkable row."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(
            f"talkable conversation not found: {conversation_id}"
        )
        self.conversation_id = conversation_id


@dataclasses.dataclass(frozen=True)
class _TalkContext:
    conversation: Conversation
    agent_id: str
    workspace: str | None


def talk_metadata_is_closing(metadata: Mapping[str, Any] | None) -> bool:
    """Return whether talk metadata refuses new turns."""
    values = metadata or {}
    return (
        values.get("talk_closing") is True
        or values.get("talk_closed") is True
    )


def _profile_project(session: Session, agent_id: str | None) -> str | None:
    if not agent_id:
        return None
    profile = session.scalars(
        select(AgentProfile)
        .where(AgentProfile.agent_id == agent_id)
        .order_by(AgentProfile.revision.desc())
        .limit(1)
    ).one_or_none()
    if profile is None:
        return None
    project = (profile.runner_config or {}).get("project")
    return project if isinstance(project, str) and project else None


def resolve_effective_execution(
    session: Session,
    *,
    agent_id: str | None,
    workspace: str | None,
    default_agent_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Return the agent and project a worker will actually use."""
    effective_agent = agent_id or default_agent_id
    effective_workspace = workspace
    if effective_workspace is None and effective_agent is not None:
        effective_workspace = _profile_project(session, effective_agent)
    return effective_agent, effective_workspace


def _effective_task_workspace(session: Session, task: Task) -> str | None:
    metadata = task.meta or {}
    stored = metadata.get("execution_workspace")
    if isinstance(stored, str) and stored:
        return stored
    if task.workspace is not None:
        return task.workspace
    return _profile_project(session, task.agent_id)


def task_occupies_workspace(
    session: Session,
    *,
    agent_id: str,
    workspace: str | None,
) -> bool:
    """Return whether a live task owns ``workspace`` or ``agent_id``."""
    statement = select(Task).where(
        Task.status.in_(("in_progress", "ready_to_commit"))
    )
    for task in session.scalars(statement):
        metadata = task.meta or {}
        if (
            task.status == "in_progress"
            and metadata.get("execution_status") != "running"
        ):
            continue
        task_agent = metadata.get("execution_agent_id") or task.agent_id
        if agent_id and task_agent == agent_id:
            return True
        task_workspace = _effective_task_workspace(session, task)
        if workspace and task_workspace == workspace:
            return True
    return False


def talk_occupies_workspace(
    session: Session,
    *,
    conversation_id: str,
    agent_id: str,
    workspace: str | None,
) -> bool:
    """Return whether another active talk owns ``workspace`` or ``agent_id``."""
    active = session.scalars(
        select(Conversation).where(
            Conversation.id != conversation_id,
            func.json_extract(Conversation.meta, "$.talk_active") == 1,
        )
    )
    for conversation in active:
        metadata = conversation.meta or {}
        if agent_id and metadata.get("talkable_agent") == agent_id:
            return True
        other_workspace = metadata.get("talk_workspace")
        if workspace and other_workspace == workspace:
            return True
    return False


def active_talk_locks(session: Session) -> tuple[set[str], set[str]]:
    """Return agent ids and workspace names locked by active talks."""
    active = session.scalars(
        select(Conversation).where(
            func.json_extract(Conversation.meta, "$.talk_active") == 1
        )
    )
    agents: set[str] = set()
    workspaces: set[str] = set()
    for conversation in active:
        metadata = conversation.meta or {}
        agent_id = metadata.get("talkable_agent")
        if isinstance(agent_id, str) and agent_id:
            agents.add(agent_id)
        workspace = metadata.get("talk_workspace")
        if isinstance(workspace, str) and workspace:
            workspaces.add(workspace)
    return agents, workspaces


def task_waits_for_talk(
    session: Session,
    task: Task,
    *,
    talk_agents: set[str],
    talk_workspaces: set[str],
    default_agent_id: str | None,
) -> bool:
    """Return whether an active direct talk owns this task's workspace."""
    if not talk_agents and not talk_workspaces:
        return False
    metadata = task.meta or {}
    effective_agent = (
        metadata.get("execution_agent_id")
        or task.agent_id
        or default_agent_id
    )
    if effective_agent is not None and effective_agent in talk_agents:
        return True
    effective_workspace = metadata.get("execution_workspace")
    if not isinstance(effective_workspace, str) or not effective_workspace:
        effective_workspace = task.workspace
    if effective_workspace is None and effective_agent is not None:
        effective_workspace = _profile_project(session, effective_agent)
    return (
        effective_workspace is not None
        and effective_workspace in talk_workspaces
    )


class WorkspaceTalkRepository:
    """Atomic start, turn acquisition and end for direct-agent talks."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def _context(
        self, session: Session, conversation_id: str
    ) -> _TalkContext:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise ConversationTalkNotFoundError(conversation_id)
        metadata = dict(conversation.meta or {})
        agent_id = metadata.get("talkable_agent")
        if not isinstance(agent_id, str) or not agent_id:
            raise ConversationTalkNotFoundError(conversation_id)
        workspace = metadata.get("talk_workspace")
        return _TalkContext(
            conversation=conversation,
            agent_id=agent_id,
            workspace=workspace if isinstance(workspace, str) else None,
        )

    def start(self, conversation_id: str) -> Conversation:
        """Activate a talk now, or mark it pending while the workspace is busy.

        Selection never launches a Runner. When a task owns the workspace the
        conversation receives ``talk_pending`` and the first turn waits in the
        dispatcher until acquisition succeeds.
        """
        with self._database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            context = self._context(session, conversation_id)
            metadata = dict(context.conversation.meta or {})
            if metadata.get("talk_active") is True:
                return context.conversation
            if metadata.get("talk_closing") is True:
                return context.conversation
            now = utcnow()
            metadata["talk_closed"] = False
            if task_occupies_workspace(
                session,
                agent_id=context.agent_id,
                workspace=context.workspace,
            ) or talk_occupies_workspace(
                session,
                conversation_id=conversation_id,
                agent_id=context.agent_id,
                workspace=context.workspace,
            ):
                metadata["talk_active"] = False
                metadata["talk_closing"] = False
                metadata["talk_pending"] = True
                metadata.setdefault(
                    "talk_pending_since", now.isoformat()
                )
            else:
                metadata["talk_active"] = True
                metadata["talk_pending"] = False
                metadata["talk_closing"] = False
                metadata.setdefault(
                    "talk_started_at", now.isoformat()
                )
                if metadata.get("talk_ended_at"):
                    metadata["talk_resumed_at"] = now.isoformat()
                metadata.pop("talk_ended_at", None)
            context.conversation.meta = metadata
            context.conversation.updated_at = now
            session.flush()
            session.refresh(context.conversation)
            return context.conversation

    def acquire_for_turn(self, conversation_id: str) -> Conversation:
        """Atomically acquire the workspace before a talk turn runs.

        The dispatcher calls the handler only after claiming the turn. This
        method is the turn-level gate: it either confirms the lock is already
        held, takes it now, or raises :class:`WorkspaceBusyError` so the
        dispatcher can put the turn back into ``pending`` without failing it.
        """
        with self._database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            context = self._context(session, conversation_id)
            metadata = dict(context.conversation.meta or {})
            if metadata.get("talk_active") is True:
                # A turn that was already queued before ``request_end`` is
                # processed; only new turns are refused at enqueue time.
                return context.conversation
            if (
                metadata.get("talk_closing") is True
                or metadata.get("talk_closed") is True
            ):
                raise ConversationTalkClosedError(conversation_id)
            if task_occupies_workspace(
                session,
                agent_id=context.agent_id,
                workspace=context.workspace,
            ) or talk_occupies_workspace(
                session,
                conversation_id=conversation_id,
                agent_id=context.agent_id,
                workspace=context.workspace,
            ):
                raise WorkspaceBusyError(
                    conversation_id, context.workspace
                )
            now = utcnow()
            metadata["talk_active"] = True
            metadata["talk_pending"] = False
            metadata["talk_closing"] = False
            metadata["talk_closed"] = False
            metadata.setdefault("talk_started_at", now.isoformat())
            if metadata.get("talk_ended_at"):
                metadata["talk_resumed_at"] = now.isoformat()
            metadata.pop("talk_ended_at", None)
            context.conversation.meta = metadata
            context.conversation.updated_at = now
            session.flush()
            session.refresh(context.conversation)
            return context.conversation

    def request_end(self, conversation_id: str) -> Conversation:
        """Stop accepting turns but keep the workspace lock until idle."""
        with self._database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            context = self._context(session, conversation_id)
            metadata = dict(context.conversation.meta or {})
            if (
                metadata.get("talk_active") is not True
                and metadata.get("talk_pending") is not True
            ):
                return context.conversation
            now = utcnow()
            metadata["talk_closing"] = True
            metadata.setdefault("talk_close_requested_at", now.isoformat())
            context.conversation.meta = metadata
            context.conversation.updated_at = now
            session.flush()
            session.refresh(context.conversation)
            return context.conversation

    def finalize_if_idle(self, conversation_id: str) -> bool:
        """Release the lock when a closing talk has no pending/running turns."""
        with self._database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return False
            metadata = dict(conversation.meta or {})
            if metadata.get("talk_closing") is not True:
                return False
            active_turns = session.scalar(
                select(func.count(Turn.id)).where(
                    Turn.conversation_id == conversation_id,
                    Turn.status.in_(("pending", "running")),
                )
            )
            if active_turns:
                return False
            now = utcnow()
            metadata["talk_active"] = False
            metadata["talk_closing"] = False
            metadata["talk_pending"] = False
            metadata["talk_closed"] = True
            metadata["talk_ended_at"] = now.isoformat()
            conversation.meta = metadata
            conversation.updated_at = now
            session.flush()
            return True

    def is_closing(self, conversation_id: str) -> bool:
        """Return whether the conversation refuses new agent turns."""
        with self._database.session() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return False
            return talk_metadata_is_closing(conversation.meta)

    def is_talkable(self, conversation_id: str) -> bool:
        """Return whether the conversation requests a direct agent."""
        with self._database.session() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return False
            return bool((conversation.meta or {}).get("talkable_agent"))
