"""Request and response schemas for the task HTTP API.

Field names mirror the storage model. ``status`` is a free-form string and
``metadata`` is arbitrary supplemental JSON, so no length caps or enum
restrictions are imposed here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..storage.models import AgentProfile, Conversation, Message, Task, Turn


class TaskCreate(BaseModel):
    """Payload accepted by ``POST /tasks``."""

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str | None = None
    status: str = "incoming"
    agent_id: str | None = None
    workspace: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class TaskImportItem(TaskCreate):
    """One task inside a ``POST /imports/tasks`` batch.

    The stable ``id`` is required because it is the idempotency key. Extra
    top-level fields are accepted (not restricted) and folded into the stored
    ``metadata`` so execution provenance -- role, provider/model, Runner
    session and artifact links, validation report, usage and original
    timestamps -- is retained without a schema change. Explicit ``metadata``
    keys win over same-named top-level extras.
    """

    model_config = ConfigDict(extra="allow")

    id: str


class TaskImportRequest(BaseModel):
    """Payload accepted by ``POST /imports/tasks``.

    No batch size cap is imposed; the caller decides how much history to send.
    """

    tasks: list[TaskImportItem]


class TaskImportResponse(BaseModel):
    """Response returned by ``POST /imports/tasks``.

    ``created`` counts tasks inserted by this request; ``skipped`` counts ids
    that already existed (or repeated earlier in the same batch).
    """

    created: int
    skipped: int


class TaskRead(BaseModel):
    """Task representation returned by the API."""

    id: str
    title: str
    description: str | None
    status: str
    agent_id: str | None
    workspace: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    metadata: dict[str, Any]


def import_item_to_record(item: TaskImportItem) -> dict[str, Any]:
    """Flatten one import item into the mapping the repository consumes.

    Top-level extra fields are merged into ``metadata`` without overriding
    keys the caller placed in ``metadata`` explicitly.
    """
    metadata = dict(item.metadata)
    for key, value in (item.model_extra or {}).items():
        metadata.setdefault(key, value)
    return {
        "id": item.id,
        "title": item.title,
        "description": item.description,
        "status": item.status,
        "workspace": item.workspace,
        "agent_id": item.agent_id,
        "metadata": metadata,
    }


def task_to_read(task: Task) -> TaskRead:
    """Convert a storage :class:`Task` into the API representation.

    The ORM attribute is ``meta`` because ``metadata`` is reserved by
    SQLAlchemy; the API surface exposes ``metadata``.
    """
    return TaskRead(
        id=task.id,
        title=task.title,
        description=task.description,
        status=task.status,
        workspace=task.workspace,
        agent_id=task.agent_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        metadata=task.meta,
    )


#: Fields PATCH /tasks/{task_id} may update; id, created_at and updated_at
#: are intentionally absent.
_TASK_MUTABLE_FIELDS = (
    "title",
    "description",
    "status",
    "workspace",
    "agent_id",
    "metadata",
)


class TaskPatch(BaseModel):
    """Payload accepted by ``PATCH /tasks/{task_id}``.

    All fields are optional. Omitted fields are left unchanged; supplied fields
    replace the stored value. ``metadata`` replaces the entire stored metadata
    mapping.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    status: str | None = None
    agent_id: str | None = None
    workspace: str | None = None
    metadata: dict[str, Any] | None = None

    def replacements(self) -> dict[str, Any]:
        """Return only the supplied mutable fields for the repository call."""
        return {
            field_name: getattr(self, field_name)
            for field_name in _TASK_MUTABLE_FIELDS
            if field_name in self.model_fields_set
        }


class ConversationCreate(BaseModel):
    """Payload accepted by ``POST /conversations``.

    ``title`` and ``metadata`` are free-form; no length caps or field
    restrictions are imposed.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class ConversationRead(BaseModel):
    """Conversation representation returned by the API."""

    id: str
    title: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    metadata: dict[str, Any]


class MessageCreate(BaseModel):
    """Payload accepted by ``POST /conversations/{id}/messages``.

    ``role``, ``text`` and ``source`` are free-form strings with no caps or
    allowlists. ``role`` defaults to ``"user"`` and ``source`` to ``"api"``.
    ``external_id`` is the optional idempotency key: retrying an identical
    payload returns the stored message.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    role: str = "user"
    source: str = "api"
    external_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageRead(BaseModel):
    """Message representation returned by the API."""

    sequence: int
    id: str
    conversation_id: str
    role: str
    text: str
    source: str
    external_id: str | None
    created_at: dt.datetime
    metadata: dict[str, Any]


class MessageAppendResponse(MessageRead):
    """Message plus replay information for ``POST .../messages``.

    ``replayed`` is true when an exact idempotent retry returned an existing
    message and wrote no new row. The status code also distinguishes the two
    outcomes: ``201 Created`` for an insertion and ``200 OK`` for a replay.
    """

    replayed: bool


def conversation_to_read(conversation: Conversation) -> ConversationRead:
    """Convert a storage :class:`Conversation` into the API representation."""
    return ConversationRead(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        metadata=conversation.meta,
    )


def message_to_read(message: Message) -> MessageRead:
    """Convert a storage :class:`Message` into the API representation."""
    return MessageRead(
        sequence=message.sequence,
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        text=message.text,
        source=message.source,
        external_id=message.external_id,
        created_at=message.created_at,
        metadata=message.meta,
    )


def message_append_to_response(
    message: Message, *, replayed: bool
) -> MessageAppendResponse:
    """Convert a :class:`Message` plus replay flag into the API response."""
    return MessageAppendResponse(
        **message_to_read(message).model_dump(),
        replayed=replayed,
    )


class AgentCreate(BaseModel):
    """Payload accepted by ``POST /agents``.

    ``agent_id`` is the caller-supplied identity and becomes revision ``1``.
    ``name``, ``role`` and ``system_prompt`` are free-form strings;
    ``runner_config`` and ``knowledge`` are arbitrary JSON objects and
    ``skills`` is a JSON array of binding objects. Each binding is a JSON
    object (the storage shape), but its keys are arbitrary; no length caps,
    role allowlists or skill-count limits are imposed.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    name: str
    role: str
    system_prompt: str
    runner_config: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    knowledge: dict[str, Any] = Field(default_factory=dict)


class AgentRead(BaseModel):
    """One immutable agent-profile snapshot returned by the API."""

    agent_id: str
    revision: int
    name: str
    role: str
    system_prompt: str
    runner_config: dict[str, Any]
    skills: list[dict[str, Any]]
    knowledge: dict[str, Any]
    created_at: dt.datetime


#: Fields PATCH /agents/{agent_id} may replace; identity and revision are
#: intentionally absent so they cannot be supplied.
_AGENT_MUTABLE_FIELDS = (
    "name",
    "role",
    "system_prompt",
    "runner_config",
    "skills",
    "knowledge",
)


class AgentPatch(BaseModel):
    """Payload accepted by ``PATCH /agents/{agent_id}``.

    ``expected_revision`` is required and must match the current revision;
    the server then appends ``expected_revision + 1``. Omitted fields are
    copied from the current snapshot; a supplied field replaces the stored
    value whole (never a deep merge). A body carrying only
    ``expected_revision`` is accepted and appends an unchanged snapshot.
    ``skills`` is validated as an array of JSON objects (the storage shape);
    its binding keys remain arbitrary. ``agent_id`` and ``revision`` are not
    part of this schema, so ``extra="forbid"`` makes an attempt to change
    identity or revision a ``422`` validation error.
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    name: str | None = None
    role: str | None = None
    system_prompt: str | None = None
    runner_config: dict[str, Any] | None = None
    skills: list[dict[str, Any]] | None = None
    knowledge: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> "AgentPatch":
        for field_name in _AGENT_MUTABLE_FIELDS:
            if field_name not in self.model_fields_set:
                continue
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must not be null")
        return self

    def replacements(self) -> dict[str, Any]:
        """Return only the supplied mutable fields, for the repository call."""
        return {
            field_name: getattr(self, field_name)
            for field_name in _AGENT_MUTABLE_FIELDS
            if field_name in self.model_fields_set
        }


def agent_to_read(profile: AgentProfile) -> AgentRead:
    """Convert a storage :class:`AgentProfile` into the API representation."""
    return AgentRead(
        agent_id=profile.agent_id,
        revision=profile.revision,
        name=profile.name,
        role=profile.role,
        system_prompt=profile.system_prompt,
        runner_config=profile.runner_config,
        skills=profile.skills,
        knowledge=profile.knowledge,
        created_at=profile.created_at,
    )


class TurnCreate(BaseModel):
    """Payload accepted by ``POST /conversations/{conversation_id}/turns``."""

    model_config = ConfigDict(extra="forbid")

    input_message_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnRead(BaseModel):
    """Turn representation returned by the API."""

    id: str
    conversation_id: str
    input_message_id: str
    status: str
    claim_token: str | None = None
    reply_message_id: str | None = None
    error: str | None = None
    attempt_count: int = 0
    created_at: dt.datetime
    updated_at: dt.datetime
    claimed_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    metadata: dict[str, Any]


def turn_to_read(turn: Turn) -> TurnRead:
    """Convert a storage :class:`Turn` into the API representation."""
    return TurnRead(
        id=turn.id,
        conversation_id=turn.conversation_id,
        input_message_id=turn.input_message_id,
        status=turn.status,
        claim_token=turn.claim_token,
        reply_message_id=turn.reply_message_id,
        error=turn.error,
        attempt_count=turn.attempt_count,
        created_at=turn.created_at,
        updated_at=turn.updated_at,
        claimed_at=turn.claimed_at,
        finished_at=turn.finished_at,
        metadata=turn.meta,
    )

