"""Durable task repository backed by SQLite.

The repository is a thin, explicit boundary: it maps plain Python arguments to
:class:`~agent_manager.storage.models.Task` rows and back. Business rules
(status values, field lengths) are deliberately not enforced here.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from .database import Database
from .models import Conversation, Message, Task, utcnow

#: Sentinel distinguishing "field omitted" from "field supplied as None".
_UNSET: Any = object()


class TaskNotFoundError(Exception):
    """Raised when an operation targets a task that is absent."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"task not found: {task_id}")
        self.task_id = task_id


class DuplicateTaskIdError(Exception):
    """Raised when a caller supplies a task ``id`` that already exists."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"task id already exists: {task_id}")
        self.task_id = task_id


def _is_task_id_conflict(error: IntegrityError) -> bool:
    """Return whether ``error`` is a duplicate ``tasks.id`` violation.

    Only the primary-key conflict is translated into
    :class:`DuplicateTaskIdError`; every other database failure propagates
    unchanged so unrelated errors are never hidden.
    """
    original = error.orig
    message = str(original)
    if "tasks.id" not in message or "UNIQUE constraint failed" not in message:
        return False
    code = getattr(original, "sqlite_errorcode", None)
    if code is not None and code not in (
        sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
        sqlite3.SQLITE_CONSTRAINT_UNIQUE,
    ):
        return False
    return True


def _is_conversation_id_conflict(error: IntegrityError) -> bool:
    """Return whether ``error`` is a duplicate ``conversations.id`` violation.

    Unrelated database failures propagate unchanged.
    """
    original = error.orig
    message = str(original)
    if (
        "conversations.id" not in message
        or "UNIQUE constraint failed" not in message
    ):
        return False
    code = getattr(original, "sqlite_errorcode", None)
    if code is not None and code not in (
        sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
        sqlite3.SQLITE_CONSTRAINT_UNIQUE,
    ):
        return False
    return True


def _is_message_conversation_fk_conflict(error: IntegrityError) -> bool:
    """Return whether ``error`` is a message->conversation foreign-key failure.

    Appending to an absent conversation is translated into
    :class:`ConversationNotFoundError`; every other integrity failure
    propagates unchanged.
    """
    original = error.orig
    code = getattr(original, "sqlite_errorcode", None)
    if code is not None:
        return code == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY
    return "FOREIGN KEY constraint failed" in str(original)


@dataclasses.dataclass(frozen=True)
class TaskImportResult:
    """Created/skipped outcome of one idempotent import batch."""

    created: int
    skipped: int


@dataclasses.dataclass(frozen=True)
class MessageAppendResult:
    """Outcome of one message append.

    ``replayed`` is true when an existing message was returned because the
    idempotency key matched an exact semantic retry; ``inserted`` is its
    negation. The stored :class:`Message` is available as :attr:`message`.
    """

    message: Message
    replayed: bool

    @property
    def inserted(self) -> bool:
        return not self.replayed


class ConversationNotFoundError(Exception):
    """Raised when appending a message to a conversation that is absent."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(f"conversation not found: {conversation_id}")
        self.conversation_id = conversation_id


class DuplicateConversationIdError(Exception):
    """Raised when a caller supplies a conversation ``id`` that exists."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(f"conversation id already exists: {conversation_id}")
        self.conversation_id = conversation_id


class MessageConflictError(Exception):
    """Raised when an idempotency key exists with a different payload.

    The original stored message is preserved and exposed as :attr:`existing`.
    The retry payload is the semantic pair ``role``/``text`` plus ``metadata``;
    generated ids and timestamps are excluded from the comparison.
    """

    def __init__(self, existing: Message) -> None:
        super().__init__(
            "message idempotency conflict for "
            f"conversation={existing.conversation_id!r} "
            f"source={existing.source!r} "
            f"external_id={existing.external_id!r}"
        )
        self.existing = existing


def _message_payload_matches(
    message: Message, role: str, text: str, meta: dict[str, Any]
) -> bool:
    """Return whether a stored message matches the semantic retry payload."""
    return (
        message.role == role
        and message.text == text
        and (message.meta or {}) == meta
    )


class TaskRepository:
    """Create, fetch and list tasks without losing committed rows.

    Args:
        database: The :class:`~agent_manager.storage.database.Database` that
            owns the SQLite file. Each repository call opens a short
            transaction on its own connection.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        *,
        title: str,
        description: str | None = None,
        status: str = "incoming",
        workspace: str | None = None,
        agent_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        id: str | None = None,
    ) -> Task:
        """Insert one task and return the persisted record.

        Args:
            title: Short task title (free text, no length cap).
            description: Optional longer description.
            status: Free-form status string; defaults to ``"incoming"``.
            workspace: Optional workspace/project identifier.
            agent_id: Explicit executor; None preserves the worker default.
            metadata: Arbitrary supplemental JSON data.
            id: Optional client-supplied identifier. A random UUID hex string
                is generated when omitted.

        Returns:
            The detached :class:`Task` with server-populated timestamps.
        """
        task = Task(
            id=id if id is not None else uuid.uuid4().hex,
            title=title,
            description=description,
            status=status,
            workspace=workspace,
            agent_id=agent_id,
            meta=dict(metadata) if metadata is not None else {},
        )
        with self._database.session() as session:
            session.add(task)
            try:
                session.flush()
            except IntegrityError as error:
                if _is_task_id_conflict(error):
                    raise DuplicateTaskIdError(task.id) from error
                raise
        return task

    def import_tasks(
        self, records: Sequence[Mapping[str, Any]]
    ) -> TaskImportResult:
        """Insert tasks whose ids do not exist yet and skip the rest.

        Each record must provide ``id`` and ``title``. Existing rows are never
        read, updated or overwritten: an ``INSERT ... ON CONFLICT DO NOTHING``
        makes the operation idempotent, so replaying the same ids leaves the
        stored rows untouched. Repeated ids inside one batch are likewise
        inserted once and skipped afterwards.

        All records are handled inside one short transaction, so an invalid
        record never leaves a partial batch behind. The atomic conflict
        handling also makes concurrent replay of the same ids safe: SQLite
        serializes the writers, the later transaction observes the committed
        row and skips it instead of raising.

        Args:
            records: Mappings with ``id``, ``title`` and optional
                ``description``, ``status``, ``workspace`` and ``metadata``.

        Returns:
            Counts of newly created and skipped records.
        """
        created = 0
        skipped = 0
        with self._database.session() as session:
            for record in records:
                statement = (
                    sqlite_insert(Task)
                    .values(
                        id=record["id"],
                        title=record["title"],
                        description=record.get("description"),
                        status=record.get("status", "incoming"),
                        workspace=record.get("workspace"),
                        agent_id=record.get("agent_id"),
                        meta=dict(record.get("metadata") or {}),
                    )
                    .on_conflict_do_nothing(index_elements=[Task.id])
                )
                result = session.execute(statement)
                if result.rowcount == 1:
                    created += 1
                else:
                    skipped += 1
        return TaskImportResult(created=created, skipped=skipped)

    def get(self, task_id: str) -> Task | None:
        """Return the task with ``task_id`` or ``None`` when absent."""
        with self._database.session() as session:
            return session.get(Task, task_id)

    def list(
        self,
        *,
        status: str | None = None,
        workspace: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        """Return tasks ordered by creation time, oldest first.

        Args:
            status: When given, only tasks with this exact status.
            workspace: When given, only tasks in this workspace.
            limit: Maximum number of rows to return.
            offset: Number of leading rows to skip.
        """
        statement = select(Task).order_by(Task.created_at, Task.id)
        if status is not None:
            statement = statement.where(Task.status == status)
        if workspace is not None:
            statement = statement.where(Task.workspace == workspace)
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        with self._database.session() as session:
            return list(session.scalars(statement))

    def update(
        self,
        task_id: str,
        *,
        title: Any = _UNSET,
        description: Any = _UNSET,
        status: Any = _UNSET,
        workspace: Any = _UNSET,
        agent_id: Any = _UNSET,
        metadata: Any = _UNSET,
    ) -> Task:
        """Update mutable fields of an existing task and return the record.

        Args:
            task_id: Identifier of the task to update.
            title: Replacement title if supplied.
            description: Replacement description if supplied (can be None).
            status: Replacement status if supplied.
            workspace: Replacement workspace if supplied (can be None).
            agent_id: Replacement executor; None restores the worker default.
            metadata: Replacement metadata mapping if supplied.

        Raises:
            TaskNotFoundError: when the task does not exist.
        """
        with self._database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if title is not _UNSET:
                task.title = str(title)
            if description is not _UNSET:
                task.description = str(description) if description is not None else None
            if status is not _UNSET:
                task.status = str(status)
            if workspace is not _UNSET:
                task.workspace = str(workspace) if workspace is not None else None
            if agent_id is not _UNSET:
                task.agent_id = agent_id
            if metadata is not _UNSET:
                task.meta = dict(metadata or {})
            task.updated_at = utcnow()
            session.flush()
            session.refresh(task)
            return task

    def claim_in_progress(
        self,
        *,
        worker_id: str,
        workspace: str | None = None,
    ) -> Task | None:
        """Atomically claim the next eligible in_progress task.

        Eligible tasks have status="in_progress" and their metadata does not
        indicate an active execution_status of "running" or "completed".
        If a candidate task specifies a workspace, it is skipped if another
        task in the same workspace is currently "ready_to_commit" or actively running.
        """
        with self._database.session() as session:
            statement = (
                select(Task)
                .where(Task.status == "in_progress")
                .order_by(Task.created_at)
            )
            if workspace is not None:
                statement = statement.where(Task.workspace == workspace)
            candidates = list(session.scalars(statement))
            for candidate in candidates:
                meta = dict(candidate.meta or {})
                if meta.get("execution_status") in ("running", "completed"):
                    continue

                if candidate.workspace:
                    # Check for any task blocking this workspace in 'ready_to_commit'
                    has_blocking_ready = session.scalar(
                        select(func.count(Task.id))
                        .where(
                            Task.workspace == candidate.workspace,
                            Task.id != candidate.id,
                            Task.status == "ready_to_commit",
                        )
                    )
                    if has_blocking_ready and has_blocking_ready > 0:
                        continue

                    # Check for any other task actively running in the same workspace
                    running_in_ws = session.scalars(
                        select(Task)
                        .where(
                            Task.workspace == candidate.workspace,
                            Task.id != candidate.id,
                            Task.status == "in_progress",
                        )
                    )
                    is_ws_busy = False
                    for other_t in running_in_ws:
                        other_meta = other_t.meta or {}
                        if other_meta.get("execution_status") == "running":
                            is_ws_busy = True
                            break
                    if is_ws_busy:
                        continue

                meta["execution_status"] = "running"
                meta["execution_worker_id"] = worker_id
                meta["execution_claimed_at"] = utcnow().isoformat()
                candidate.meta = meta
                candidate.updated_at = utcnow()
                session.flush()
                session.refresh(candidate)
                return candidate
            return None

    def complete_execution(
        self,
        task_id: str,
        *,
        status: str,
        summary: str | None = None,
        changed_files: Sequence[str] | None = None,
        error: str | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> Task:
        """Mark a claimed task execution finished and record outputs in metadata."""
        with self._database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            task.status = status
            meta = dict(task.meta or {})
            if status in ("done", "ready_to_commit"):
                meta["execution_status"] = "completed"
            elif status == "waiting_for_input":
                meta["execution_status"] = "waiting_for_input"
            else:
                meta["execution_status"] = "failed"
            meta["execution_finished_at"] = utcnow().isoformat()
            if summary is not None:
                meta["summary"] = summary
            if changed_files is not None:
                meta["changed_files"] = list(changed_files)
            if error is not None:
                meta["error"] = error
            if provenance is not None:
                meta["provenance"] = dict(provenance)
            task.meta = meta
            task.updated_at = utcnow()
            session.flush()
            session.refresh(task)
            return task



class ConversationRepository:
    """Create/fetch conversations and append/list their messages.

    Conversations and messages are stored in their own tables and are never
    turned into tasks. Each call opens a short transaction on its own
    connection. Message order is defined by the database-assigned
    ``messages.sequence`` value, not by timestamps.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def create_conversation(
        self,
        *,
        title: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        id: str | None = None,
    ) -> Conversation:
        """Insert one conversation and return the persisted record.

        Args:
            title: Optional free-form title.
            metadata: Arbitrary supplemental JSON data.
            id: Optional client-supplied identifier. A random UUID hex string
                is generated when omitted. A duplicate id raises
                :class:`DuplicateConversationIdError`.
        """
        conversation = Conversation(
            id=id if id is not None else uuid.uuid4().hex,
            title=title,
            meta=dict(metadata) if metadata is not None else {},
        )
        with self._database.session() as session:
            session.add(conversation)
            try:
                session.flush()
            except IntegrityError as error:
                if _is_conversation_id_conflict(error):
                    raise DuplicateConversationIdError(conversation.id) from error
                raise
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Return the conversation with ``conversation_id`` or ``None``."""
        with self._database.session() as session:
            return session.get(Conversation, conversation_id)

    def list_conversations(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[Conversation]:
        """Return conversations ordered by creation time, oldest first."""
        statement = select(Conversation).order_by(
            Conversation.created_at, Conversation.id
        )
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        with self._database.session() as session:
            return list(session.scalars(statement))

    def append_message(
        self,
        *,
        conversation_id: str,
        role: str,
        text: str,
        source: str = "api",
        external_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MessageAppendResult:
        """Append one message to a conversation, idempotently.

        When ``external_id`` is present, ``(conversation_id, source,
        external_id)`` is an idempotency key. An exact semantic retry
        (``role``/``text``/``metadata``) returns the stored message with
        ``replayed=True``; a different payload raises
        :class:`MessageConflictError` and leaves the original untouched. When
        ``external_id`` is ``None`` every call inserts a new message.

        The insert uses an atomic ``INSERT ... ON CONFLICT DO NOTHING`` so
        concurrent delivery of the same key serializes at SQLite and never
        surfaces a raw integrity failure. Appending to an absent conversation
        raises :class:`ConversationNotFoundError`.
        """
        normalized_meta = dict(metadata) if metadata is not None else {}
        message_id = uuid.uuid4().hex
        with self._database.session() as session:
            statement = (
                sqlite_insert(Message)
                .values(
                    id=message_id,
                    conversation_id=conversation_id,
                    role=role,
                    text=text,
                    source=source,
                    external_id=external_id,
                    meta=normalized_meta,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        Message.conversation_id,
                        Message.source,
                        Message.external_id,
                    ]
                )
            )
            try:
                session.execute(statement)
            except IntegrityError as error:
                if _is_message_conversation_fk_conflict(error):
                    raise ConversationNotFoundError(conversation_id) from error
                raise

            inserted = session.scalars(
                select(Message).where(Message.id == message_id)
            ).one_or_none()
            if inserted is not None:
                session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(updated_at=utcnow())
                )
                return MessageAppendResult(message=inserted, replayed=False)

            existing = session.scalars(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.source == source,
                    Message.external_id == external_id,
                )
            ).one()
            if not _message_payload_matches(
                existing, role, text, normalized_meta
            ):
                # The session rolls back because we raise; detach the original
                # first so its loaded attributes stay readable on the error.
                session.expunge(existing)
                raise MessageConflictError(existing)
            return MessageAppendResult(message=existing, replayed=True)

    def get_message(self, message_id: str) -> Message | None:
        """Return the message with the stable ``message_id`` or ``None``.

        ``message_id`` is the repository-assigned string identifier, not the
        database ``sequence`` primary key.
        """
        with self._database.session() as session:
            return session.scalars(
                select(Message).where(Message.id == message_id)
            ).one_or_none()

    def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Message]:
        """Return a conversation's messages ordered by ``sequence``.

        An absent conversation yields an empty list; appending is the only
        operation that reports a typed not-found error.
        """
        statement = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence)
        )
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        with self._database.session() as session:
            return list(session.scalars(statement))
