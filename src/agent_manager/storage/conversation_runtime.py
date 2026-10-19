"""Durable secretary conversation/session binding.

One :class:`~agent_manager.storage.models.ConversationRuntime` row belongs to
exactly one conversation. It pins the profile snapshot that the conversation's
native Runner session was first prepared from, remembers the exported paths
and the native session id, and owns the single model-call reservation that may
exist for that conversation at a time.

The reservation protocol is intentionally small and SQL-conditional:

* :meth:`ConversationRuntimeRepository.reserve_call` validates that the turn
  is currently ``running`` at the exact ``attempt_count`` and writes the
  opaque ownership token with a conditional ``UPDATE ... WHERE``. Two real
  connections that race on the same conversation therefore produce exactly one
  owner; the loser observes the committed token and receives
  :class:`ConversationRuntimeBusyError`.
* If the turn is already the binding's last successful turn, the cached reply
  and provenance are returned without writing a reservation, so a crash after
  the result is stored but before ``TurnProcessor.complete`` does not call the
  model again or advance the native session twice.
* :meth:`attach_prepared` records the export paths and Runner state directory
  once, under the current ownership token. Values already present are
  preserved.
* :meth:`finish_call` atomically advances the native session id, stores the
  last successful turn/reply/provenance and releases the reservation, but only
  while the caller still owns the reservation and the original turn is still
  ``running`` at the reserved attempt.
* :meth:`block_call` releases the reservation into a blocked state with a
  diagnostic reason. Blocked or abandoned reservations refuse automatic
  replay and fresh-session fallback; there is no automatic unlock or retry
  operation here.

This module stores one last reply only. It is not a full message archive and
not a generic execution journal. Recovery/reconciliation of an uncertain model
call is future work.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .database import Database
from .models import (
    AgentProfile,
    Conversation,
    ConversationRuntime,
    Turn,
    utcnow,
)

__all__ = [
    "ConversationRuntime",
    "ConversationRuntimeRepository",
    "INTERRUPTED_BLOCKED_PREFIX",
    "RuntimeReservation",
    "clear_interrupted_block",
    "interrupted_blocked_reason",
    "is_interrupted_block",
    "release_interrupted_reservation",
]

#: Number of fresh attempts when SQLite reports a writer snapshot/lock
#: conflict. Each attempt opens its own short transaction.
_MAX_RUNTIME_ATTEMPTS = 5

#: Prefix of the fail-closed block recorded when startup reconciliation
#: interrupts an in-flight model call. Only blocks carrying this marker are
#: cleared by an explicit retry; a block caused by an uncertain call stays
#: latched.
INTERRUPTED_BLOCKED_PREFIX = "Interrupted: "


def interrupted_blocked_reason(reason: str) -> str:
    """Return the fail-closed reason recorded for an interrupted call."""
    return f"{INTERRUPTED_BLOCKED_PREFIX}{reason}"


def is_interrupted_block(blocked_reason: str | None) -> bool:
    """Return whether ``blocked_reason`` marks a startup interruption."""
    return blocked_reason is not None and blocked_reason.startswith(
        INTERRUPTED_BLOCKED_PREFIX
    )


def release_interrupted_reservation(
    session: Session,
    *,
    conversation_id: str,
    turn_id: str,
    reason: str,
) -> bool:
    """Clear ``turn_id``'s reservation and latch the interrupted block.

    The conditional write only touches a binding whose
    ``reservation_turn_id`` equals ``turn_id``. It executes inside the
    caller's session, so the reservation release commits atomically with the
    turn's failure transition. A binding without an active reservation for
    ``turn_id`` is left untouched. Returns whether a reservation was
    released.
    """
    statement = (
        update(ConversationRuntime)
        .where(
            ConversationRuntime.conversation_id == conversation_id,
            ConversationRuntime.reservation_turn_id == turn_id,
        )
        .values(
            reservation_token=None,
            reservation_turn_id=None,
            reservation_attempt_count=None,
            blocked_reason=interrupted_blocked_reason(reason),
            updated_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    return session.execute(statement).rowcount == 1


def clear_interrupted_block(
    session: Session,
    *,
    conversation_id: str,
) -> bool:
    """Clear a startup-interruption block for one conversation.

    Only a block carrying :data:`INTERRUPTED_BLOCKED_PREFIX` is cleared, so
    an uncertain-call block stays latched. It executes inside the caller's
    session, so an explicit retry reopens the turn and unblocks the
    conversation atomically. Returns whether a row was updated.
    """
    statement = (
        update(ConversationRuntime)
        .where(
            ConversationRuntime.conversation_id == conversation_id,
            ConversationRuntime.blocked_reason.like(
                f"{INTERRUPTED_BLOCKED_PREFIX}%"
            ),
        )
        .values(blocked_reason=None, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    return session.execute(statement).rowcount == 1


class ConversationRuntimeError(Exception):
    """Base class for conversation-runtime binding failures."""


class ConversationRuntimeNotFoundError(ConversationRuntimeError):
    """Raised when a conversation has no runtime binding row."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(
            f"conversation runtime not found: {conversation_id}"
        )
        self.conversation_id = conversation_id


class ConversationRuntimeReferenceError(ConversationRuntimeError):
    """Raised when a conversation, profile or turn reference is missing."""

    def __init__(self, reference: str, value: str) -> None:
        super().__init__(f"missing {reference}: {value}")
        self.reference = reference
        self.value = value


class ConversationRuntimeInvalidError(ConversationRuntimeError):
    """Base class for invalid or stale runtime ownership/state."""


class ConversationRuntimeOwnershipError(ConversationRuntimeInvalidError):
    """Raised when a caller does not own the current reservation."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(
            f"reservation token is missing, stale or not valid for "
            f"conversation {conversation_id}"
        )
        self.conversation_id = conversation_id


class ConversationRuntimeBusyError(ConversationRuntimeError):
    """Raised when the conversation already has an active reservation."""

    def __init__(
        self,
        conversation_id: str,
        *,
        reservation_turn_id: str | None = None,
        reservation_attempt_count: int | None = None,
    ) -> None:
        super().__init__(
            f"conversation {conversation_id} already has an active "
            f"reservation for turn {reservation_turn_id!r}"
        )
        self.conversation_id = conversation_id
        self.reservation_turn_id = reservation_turn_id
        self.reservation_attempt_count = reservation_attempt_count


class ConversationRuntimeBlockedError(ConversationRuntimeError):
    """Raised when the conversation binding is blocked and fails closed."""

    def __init__(self, conversation_id: str, reason: str | None) -> None:
        super().__init__(
            f"conversation {conversation_id} runtime is blocked: {reason}"
        )
        self.conversation_id = conversation_id
        self.reason = reason


class ConversationRuntimeTurnStateError(ConversationRuntimeInvalidError):
    """Raised when the turn is not running at the requested exact attempt."""

    def __init__(
        self,
        turn_id: str,
        status: str | None,
        attempt_count: int | None,
    ) -> None:
        super().__init__(
            f"turn {turn_id} is not running at attempt {attempt_count} "
            f"(status={status!r})"
        )
        self.turn_id = turn_id
        self.status = status
        self.attempt_count = attempt_count


class ConversationRuntimeAgentConflictError(ConversationRuntimeInvalidError):
    """Raised when a conversation already binds to a different agent."""

    def __init__(
        self,
        conversation_id: str,
        pinned_agent_id: str,
        requested_agent_id: str,
    ) -> None:
        super().__init__(
            f"conversation {conversation_id} is pinned to agent "
            f"{pinned_agent_id!r}, not {requested_agent_id!r}"
        )
        self.conversation_id = conversation_id
        self.pinned_agent_id = pinned_agent_id
        self.requested_agent_id = requested_agent_id


class ConversationRuntimeAttachmentError(ConversationRuntimeInvalidError):
    """Raised when prepared export paths are attached more than once."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(
            f"prepared export paths are already attached for conversation "
            f"{conversation_id}"
        )
        self.conversation_id = conversation_id


class ConversationRuntimeSessionConflictError(ConversationRuntimeInvalidError):
    """Raised when a resumed session would silently switch its session id."""

    def __init__(
        self,
        conversation_id: str,
        existing_session_id: str | None,
        requested_session_id: str | None,
    ) -> None:
        super().__init__(
            f"conversation {conversation_id} native session "
            f"{existing_session_id!r} cannot be replaced by "
            f"{requested_session_id!r}"
        )
        self.conversation_id = conversation_id
        self.existing_session_id = existing_session_id
        self.requested_session_id = requested_session_id


@dataclasses.dataclass(frozen=True)
class RuntimeReservation:
    """Outcome of :meth:`ConversationRuntimeRepository.reserve_call`.

    ``reserved`` is true when a new opaque ownership token now guards the
    conversation. ``cached`` is true when no reservation was needed because
    the turn is already the last successfully processed turn; in that case
    :attr:`reply_text` and :attr:`provenance` hold the stored result and
    :attr:`ownership_token` is ``None``.
    """

    binding: ConversationRuntime
    reserved: bool
    cached: bool
    ownership_token: str | None = None
    reply_text: str | None = None
    provenance: Mapping[str, Any] | None = None

    @property
    def token(self) -> str | None:
        """Alias for :attr:`ownership_token`."""
        return self.ownership_token

    @property
    def reply(self) -> str | None:
        """Alias for :attr:`reply_text`."""
        return self.reply_text


def _is_sqlite_busy(error: OperationalError) -> bool:
    """Return whether ``error`` is a retryable SQLite busy/snapshot failure."""
    original = error.orig
    code = getattr(original, "sqlite_errorcode", None)
    busy_codes = {sqlite3.SQLITE_BUSY}
    snapshot_code = getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", None)
    if snapshot_code is not None:
        busy_codes.add(snapshot_code)
    if code is not None and code in busy_codes:
        return True
    return "database is locked" in str(original).lower()


def _run_with_retry(operation: Any) -> Any:
    """Run ``operation`` with fresh transactions during SQLite lock races."""
    last_error: OperationalError | None = None
    for attempt in range(_MAX_RUNTIME_ATTEMPTS):
        try:
            return operation()
        except OperationalError as error:
            last_error = error
            if not _is_sqlite_busy(error):
                raise
            if attempt + 1 >= _MAX_RUNTIME_ATTEMPTS:
                raise
    raise last_error  # pragma: no cover - loop always returns or raises


def _resolve_agent_id(
    agent_id: str | None, profile_id: str | None
) -> str:
    """Return the single requested agent id, or raise for ambiguous input."""
    values = [value for value in (agent_id, profile_id) if value is not None]
    if not values:
        raise TypeError("agent_id is required")
    if len(set(values)) > 1:
        raise TypeError("pass either agent_id or profile_id, not both")
    return values[0]


def _resolve_revision(
    profile_revision: int | None, revision: int | None
) -> int | None:
    """Return the single requested revision, or raise for ambiguous input."""
    if (
        profile_revision is not None
        and revision is not None
        and profile_revision != revision
    ):
        raise TypeError(
            "pass either profile_revision or revision, not both"
        )
    return profile_revision if profile_revision is not None else revision


def _validate_session_id(
    session_id: Any, conversation_id: str
) -> str:
    """Return a non-empty native session id or raise a typed error."""
    if not isinstance(session_id, str) or session_id == "":
        raise ConversationRuntimeSessionConflictError(
            conversation_id, None, session_id
        )
    return session_id


class ConversationRuntimeRepository:
    """Read and mutate the per-conversation secretary session binding.

    Args:
        database: The :class:`~agent_manager.storage.database.Database` that
            owns the SQLite file. Every call opens its own short transaction
            on its own connection; no Python-level lock is used.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # -- reads -----------------------------------------------------------

    def get_binding(
        self, conversation_id: str
    ) -> ConversationRuntime | None:
        """Return the binding for ``conversation_id`` or ``None``."""
        with self._database.session() as session:
            return session.get(ConversationRuntime, conversation_id)

    #: Alias for :meth:`get_binding`.
    read_binding = get_binding
    #: Alias for :meth:`get_binding`.
    get_runtime = get_binding
    #: Alias for :meth:`get_binding`.
    read = get_binding
    #: Alias for :meth:`get_binding`.
    get = get_binding

    # -- reserve ---------------------------------------------------------

    def reserve_call(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        attempt_count: int,
        agent_id: str | None = None,
        profile_revision: int | None = None,
        profile_id: str | None = None,
        revision: int | None = None,
    ) -> RuntimeReservation:
        """Reserve the conversation's single model call for a running turn.

        ``turn_id`` must belong to ``conversation_id`` and be ``running`` at
        exactly ``attempt_count``. On the first binding the selected
        ``agent_id`` and ``profile_revision`` are pinned; a later request may
        name the same agent but never overwrites the pinned revision, while a
        different agent raises
        :class:`ConversationRuntimeAgentConflictError`.

        Returns a :class:`RuntimeReservation` carrying an opaque ownership
        token and the detached binding, or a cached reservation when the turn
        is already the binding's last successful turn. A blocked binding
        raises :class:`ConversationRuntimeBlockedError` and any active
        reservation (including an abandoned one) raises
        :class:`ConversationRuntimeBusyError`.
        """
        resolved_agent = _resolve_agent_id(agent_id, profile_id)
        resolved_revision = _resolve_revision(
            profile_revision, revision
        )
        return _run_with_retry(
            lambda: self._reserve_once(
                conversation_id=conversation_id,
                turn_id=turn_id,
                attempt_count=attempt_count,
                agent_id=resolved_agent,
                profile_revision=resolved_revision,
            )
        )

    #: Aliases for :meth:`reserve_call`.
    reserve = reserve_call
    begin_call = reserve_call
    begin = reserve_call

    def _reserve_once(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        attempt_count: int,
        agent_id: str,
        profile_revision: int | None,
    ) -> RuntimeReservation:
        with self._database.session() as session:
            if session.get(Conversation, conversation_id) is None:
                raise ConversationRuntimeReferenceError(
                    "conversation", conversation_id
                )

            binding = session.get(ConversationRuntime, conversation_id)
            if binding is None:
                pinned_revision = self._resolve_pinned_revision(
                    session, agent_id, profile_revision
                )
                insert_statement = (
                    sqlite_insert(ConversationRuntime)
                    .values(
                        conversation_id=conversation_id,
                        agent_id=agent_id,
                        profile_revision=pinned_revision,
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            ConversationRuntime.conversation_id
                        ]
                    )
                )
                session.execute(insert_statement)
                session.expire_all()
                binding = session.get(
                    ConversationRuntime, conversation_id
                )
                if binding is None:  # pragma: no cover - insert or existing
                    raise ConversationRuntimeReferenceError(
                        "conversation_runtime", conversation_id
                    )

            self._raise_if_unavailable(binding, agent_id)

            turn = session.scalars(
                select(Turn).where(Turn.id == turn_id)
            ).one_or_none()
            if turn is None or turn.conversation_id != conversation_id:
                raise ConversationRuntimeReferenceError("turn", turn_id)
            if (
                turn.status != "running"
                or turn.attempt_count != attempt_count
            ):
                raise ConversationRuntimeTurnStateError(
                    turn_id, turn.status, turn.attempt_count
                )

            if binding.last_turn_id == turn_id:
                return RuntimeReservation(
                    binding=binding,
                    reserved=False,
                    cached=True,
                    ownership_token=None,
                    reply_text=binding.last_reply_text,
                    provenance=binding.last_provenance,
                )

            token = uuid.uuid4().hex
            statement = (
                update(ConversationRuntime)
                .where(
                    ConversationRuntime.conversation_id
                    == conversation_id,
                    ConversationRuntime.reservation_token.is_(None),
                    ConversationRuntime.blocked_reason.is_(None),
                    ConversationRuntime.agent_id == agent_id,
                    or_(
                        ConversationRuntime.last_turn_id.is_(None),
                        ConversationRuntime.last_turn_id != turn_id,
                    ),
                    self._turn_running_exists(
                        conversation_id, turn_id, attempt_count
                    ),
                )
                .values(
                    reservation_token=token,
                    reservation_turn_id=turn_id,
                    reservation_attempt_count=attempt_count,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            result = session.execute(statement)
            if result.rowcount != 1:
                session.expire_all()
                current = session.get(
                    ConversationRuntime, conversation_id
                )
                return self._raise_reservation_failure(
                    current, conversation_id, turn_id, agent_id
                )

            session.expire_all()
            binding = session.get(ConversationRuntime, conversation_id)
            assert binding is not None  # pragma: no cover - row exists
            return RuntimeReservation(
                binding=binding,
                reserved=True,
                cached=False,
                ownership_token=token,
            )

    @staticmethod
    def _turn_running_exists(
        conversation_id: str, turn_id: str, attempt_count: int
    ) -> Any:
        """Build the exact running-turn guard used by conditional writes."""
        return exists().where(
            Turn.id == turn_id,
            Turn.conversation_id == conversation_id,
            Turn.status == "running",
            Turn.attempt_count == attempt_count,
        )

    @staticmethod
    def _resolve_pinned_revision(
        session: Any, agent_id: str, profile_revision: int | None
    ) -> int:
        """Return the revision pinned on first binding, validating it exists."""
        if profile_revision is None:
            current = session.scalar(
                select(func.max(AgentProfile.revision)).where(
                    AgentProfile.agent_id == agent_id
                )
            )
            if current is None:
                raise ConversationRuntimeReferenceError(
                    "agent_profile", agent_id
                )
            return current
        found = session.scalar(
            select(AgentProfile.agent_id).where(
                AgentProfile.agent_id == agent_id,
                AgentProfile.revision == profile_revision,
            )
        )
        if found is None:
            raise ConversationRuntimeReferenceError(
                "agent_profile", f"{agent_id}@{profile_revision}"
            )
        return profile_revision

    def _raise_if_unavailable(
        self, binding: ConversationRuntime, agent_id: str
    ) -> None:
        """Fail closed on blocked/changed bindings before any reservation."""
        if binding.blocked_reason is not None:
            raise ConversationRuntimeBlockedError(
                binding.conversation_id, binding.blocked_reason
            )
        if binding.reservation_token is not None:
            raise ConversationRuntimeBusyError(
                binding.conversation_id,
                reservation_turn_id=binding.reservation_turn_id,
                reservation_attempt_count=binding.reservation_attempt_count,
            )
        if binding.agent_id != agent_id:
            raise ConversationRuntimeAgentConflictError(
                binding.conversation_id, binding.agent_id, agent_id
            )

    def _raise_reservation_failure(
        self,
        binding: ConversationRuntime | None,
        conversation_id: str,
        turn_id: str,
        agent_id: str,
    ) -> RuntimeReservation:
        """Translate a lost reservation race into a typed error or cache hit."""
        if binding is None:
            raise ConversationRuntimeNotFoundError(conversation_id)
        if binding.blocked_reason is not None:
            raise ConversationRuntimeBlockedError(
                binding.conversation_id, binding.blocked_reason
            )
        if binding.reservation_token is not None:
            raise ConversationRuntimeBusyError(
                binding.conversation_id,
                reservation_turn_id=binding.reservation_turn_id,
                reservation_attempt_count=binding.reservation_attempt_count,
            )
        if binding.agent_id != agent_id:
            raise ConversationRuntimeAgentConflictError(
                binding.conversation_id, binding.agent_id, agent_id
            )
        if binding.last_turn_id == turn_id:
            # Another worker stored the result and released the reservation
            # while this caller was racing; the cached result wins.
            return RuntimeReservation(
                binding=binding,
                reserved=False,
                cached=True,
                ownership_token=None,
                reply_text=binding.last_reply_text,
                provenance=binding.last_provenance,
            )
        raise ConversationRuntimeOwnershipError(binding.conversation_id)

    # -- attach ----------------------------------------------------------

    def attach_prepared(
        self,
        *,
        conversation_id: str,
        ownership_token: str,
        role_export_path: str,
        snapshot_export_path: str,
        prompt_export_path: str,
        runner_state_dir: str,
        turn_id: str | None = None,
        attempt_count: int | None = None,
    ) -> ConversationRuntime:
        """Attach prepared export paths and state directory once.

        The caller must still own the current reservation and the reserved
        turn must still be ``running`` at its reserved attempt in this same
        conversation, even when ``turn_id``/``attempt_count`` are omitted.
        If the four paths are already attached they are preserved: an
        identical retry returns the binding, while different values raise
        :class:`ConversationRuntimeAttachmentError`. A stale token never
        changes a newer reservation.
        """
        return _run_with_retry(
            lambda: self._attach_once(
                conversation_id=conversation_id,
                ownership_token=ownership_token,
                role_export_path=role_export_path,
                snapshot_export_path=snapshot_export_path,
                prompt_export_path=prompt_export_path,
                runner_state_dir=runner_state_dir,
                turn_id=turn_id,
                attempt_count=attempt_count,
            )
        )

    #: Aliases for :meth:`attach_prepared`.
    attach = attach_prepared
    attach_paths = attach_prepared

    def _attach_once(
        self,
        *,
        conversation_id: str,
        ownership_token: str,
        role_export_path: str,
        snapshot_export_path: str,
        prompt_export_path: str,
        runner_state_dir: str,
        turn_id: str | None,
        attempt_count: int | None,
    ) -> ConversationRuntime:
        with self._database.session() as session:
            binding = session.get(ConversationRuntime, conversation_id)
            if binding is None:
                raise ConversationRuntimeNotFoundError(conversation_id)
            self._require_ownership(
                binding, ownership_token, turn_id, attempt_count
            )
            reserved_turn_id = binding.reservation_turn_id
            reserved_attempt = binding.reservation_attempt_count
            if reserved_turn_id is None or reserved_attempt is None:
                raise ConversationRuntimeOwnershipError(conversation_id)
            # The reserved turn must still be running at its reserved attempt
            # even when the optional turn/attempt arguments were omitted.
            self._require_reserved_turn_running(
                session,
                conversation_id,
                reserved_turn_id,
                reserved_attempt,
            )

            requested = (
                str(role_export_path),
                str(snapshot_export_path),
                str(prompt_export_path),
                str(runner_state_dir),
            )
            existing = (
                binding.role_export_path,
                binding.snapshot_export_path,
                binding.prompt_export_path,
                binding.runner_state_dir,
            )
            if any(value is not None for value in existing):
                if existing == requested:
                    return binding
                raise ConversationRuntimeAttachmentError(conversation_id)

            statement = (
                update(ConversationRuntime)
                .where(
                    ConversationRuntime.conversation_id
                    == conversation_id,
                    ConversationRuntime.reservation_token
                    == ownership_token,
                    ConversationRuntime.role_export_path.is_(None),
                    ConversationRuntime.snapshot_export_path.is_(None),
                    ConversationRuntime.prompt_export_path.is_(None),
                    ConversationRuntime.runner_state_dir.is_(None),
                    self._turn_running_exists(
                        conversation_id,
                        reserved_turn_id,
                        reserved_attempt,
                    ),
                )
                .values(
                    role_export_path=requested[0],
                    snapshot_export_path=requested[1],
                    prompt_export_path=requested[2],
                    runner_state_dir=requested[3],
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            result = session.execute(statement)
            if result.rowcount != 1:
                session.expire_all()
                current = session.get(
                    ConversationRuntime, conversation_id
                )
                if current is None:  # pragma: no cover - row existed
                    raise ConversationRuntimeNotFoundError(conversation_id)
                self._require_ownership(
                    current, ownership_token, turn_id, attempt_count
                )
                current_turn_id = current.reservation_turn_id
                current_attempt = current.reservation_attempt_count
                if current_turn_id is None or current_attempt is None:
                    raise ConversationRuntimeOwnershipError(
                        conversation_id
                    )
                self._require_reserved_turn_running(
                    session,
                    conversation_id,
                    current_turn_id,
                    current_attempt,
                )
                current_paths = (
                    current.role_export_path,
                    current.snapshot_export_path,
                    current.prompt_export_path,
                    current.runner_state_dir,
                )
                if current_paths == requested:
                    return current
                raise ConversationRuntimeAttachmentError(conversation_id)

            session.expire_all()
            binding = session.get(ConversationRuntime, conversation_id)
            assert binding is not None  # pragma: no cover - row exists
            return binding

    # -- finish ----------------------------------------------------------

    def finish_call(
        self,
        *,
        conversation_id: str,
        ownership_token: str,
        turn_id: str,
        attempt_count: int,
        reply_text: str,
        runner_session_id: str,
        provenance: Any = None,
    ) -> ConversationRuntime:
        """Store a successful result and release the reservation atomically.

        The conditional write requires the caller's ownership token, the
        original ``turn_id``/``attempt_count`` reservation and the turn still
        being ``running`` at that exact attempt. The native session id may be
        set once; a resumed session must present the same id or the write is
        refused with :class:`ConversationRuntimeSessionConflictError`.
        """
        session_id = _validate_session_id(
            runner_session_id, conversation_id
        )
        payload = dict(provenance) if provenance is not None else None
        return _run_with_retry(
            lambda: self._finish_once(
                conversation_id=conversation_id,
                ownership_token=ownership_token,
                turn_id=turn_id,
                attempt_count=attempt_count,
                reply_text=str(reply_text),
                runner_session_id=session_id,
                provenance=payload,
            )
        )

    #: Aliases for :meth:`finish_call`.
    finish = finish_call
    finish_success = finish_call

    def _finish_once(
        self,
        *,
        conversation_id: str,
        ownership_token: str,
        turn_id: str,
        attempt_count: int,
        reply_text: str,
        runner_session_id: str,
        provenance: dict[str, Any] | None,
    ) -> ConversationRuntime:
        with self._database.session() as session:
            statement = (
                update(ConversationRuntime)
                .where(
                    ConversationRuntime.conversation_id
                    == conversation_id,
                    ConversationRuntime.reservation_token
                    == ownership_token,
                    ConversationRuntime.reservation_turn_id == turn_id,
                    ConversationRuntime.reservation_attempt_count
                    == attempt_count,
                    ConversationRuntime.blocked_reason.is_(None),
                    or_(
                        ConversationRuntime.runner_session_id.is_(None),
                        ConversationRuntime.runner_session_id
                        == runner_session_id,
                    ),
                    self._turn_running_exists(
                        conversation_id, turn_id, attempt_count
                    ),
                )
                .values(
                    runner_session_id=runner_session_id,
                    last_turn_id=turn_id,
                    last_reply_text=reply_text,
                    last_provenance=provenance,
                    reservation_token=None,
                    reservation_turn_id=None,
                    reservation_attempt_count=None,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            result = session.execute(statement)
            if result.rowcount != 1:
                session.expire_all()
                current = session.get(
                    ConversationRuntime, conversation_id
                )
                self._raise_finish_failure(
                    session,
                    current,
                    ownership_token,
                    turn_id,
                    attempt_count,
                    runner_session_id,
                )

            session.expire_all()
            binding = session.get(ConversationRuntime, conversation_id)
            assert binding is not None  # pragma: no cover - row exists
            return binding

    def _raise_finish_failure(
        self,
        session: Any,
        binding: ConversationRuntime | None,
        ownership_token: str,
        turn_id: str,
        attempt_count: int,
        runner_session_id: str,
    ) -> None:
        if binding is None:
            raise ConversationRuntimeNotFoundError("<unknown>")
        if (
            binding.reservation_token != ownership_token
            or binding.reservation_turn_id != turn_id
            or binding.reservation_attempt_count != attempt_count
        ):
            raise ConversationRuntimeOwnershipError(
                binding.conversation_id
            )
        if binding.blocked_reason is not None:
            raise ConversationRuntimeBlockedError(
                binding.conversation_id, binding.blocked_reason
            )
        if (
            binding.runner_session_id is not None
            and binding.runner_session_id != runner_session_id
        ):
            raise ConversationRuntimeSessionConflictError(
                binding.conversation_id,
                binding.runner_session_id,
                runner_session_id,
            )
        # Ownership is intact, so the original turn must no longer be running
        # at the reserved attempt (a recovered old worker cannot advance it).
        current_turn = session.scalars(
            select(Turn).where(Turn.id == turn_id)
        ).one_or_none()
        status = None if current_turn is None else current_turn.status
        attempt = (
            None if current_turn is None else current_turn.attempt_count
        )
        raise ConversationRuntimeTurnStateError(turn_id, status, attempt)

    # -- block -----------------------------------------------------------

    def block_call(
        self,
        *,
        conversation_id: str,
        ownership_token: str,
        reason: str,
        turn_id: str | None = None,
        attempt_count: int | None = None,
    ) -> ConversationRuntime:
        """Release the owned reservation into a blocked, fail-closed state.

        Ownership is required and another owner's reservation is never
        overwritten. The turn may already have been recovered: blocking does
        not depend on the turn's current status. There is no matching unlock
        operation; recovery/reconciliation of blocked uncertain calls is
        future work.
        """
        return _run_with_retry(
            lambda: self._block_once(
                conversation_id=conversation_id,
                ownership_token=ownership_token,
                reason=str(reason),
                turn_id=turn_id,
                attempt_count=attempt_count,
            )
        )

    #: Aliases for :meth:`block_call`.
    block = block_call
    block_failure = block_call

    def _block_once(
        self,
        *,
        conversation_id: str,
        ownership_token: str,
        reason: str,
        turn_id: str | None,
        attempt_count: int | None,
    ) -> ConversationRuntime:
        with self._database.session() as session:
            conditions = [
                ConversationRuntime.conversation_id == conversation_id,
                ConversationRuntime.reservation_token == ownership_token,
            ]
            if turn_id is not None:
                conditions.append(
                    ConversationRuntime.reservation_turn_id == turn_id
                )
            if attempt_count is not None:
                conditions.append(
                    ConversationRuntime.reservation_attempt_count
                    == attempt_count
                )
            statement = (
                update(ConversationRuntime)
                .where(*conditions)
                .values(
                    blocked_reason=reason,
                    reservation_token=None,
                    reservation_turn_id=None,
                    reservation_attempt_count=None,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            result = session.execute(statement)
            if result.rowcount != 1:
                session.expire_all()
                current = session.get(
                    ConversationRuntime, conversation_id
                )
                if current is None:
                    raise ConversationRuntimeNotFoundError(
                        conversation_id
                    )
                raise ConversationRuntimeOwnershipError(conversation_id)

            session.expire_all()
            binding = session.get(ConversationRuntime, conversation_id)
            assert binding is not None  # pragma: no cover - row exists
            return binding

    # -- pre-launch release ----------------------------------------------

    def release_prelaunch_call(
        self,
        *,
        conversation_id: str,
        ownership_token: str,
        turn_id: str,
        attempt_count: int,
    ) -> ConversationRuntime:
        """Release an owned reservation after a proven pre-launch failure.

        This is intentionally narrower than an unlock. It clears only the
        reservation token/turn/attempt and never touches ``blocked_reason``,
        so a blocked or abandoned reservation is refused. The caller must
        still own the reservation at ``ownership_token`` and the original
        turn must still be ``running`` at exactly ``attempt_count``. A stale
        token, a different turn/attempt or a turn that has moved on raises
        :class:`ConversationRuntimeOwnershipError` or
        :class:`ConversationRuntimeTurnStateError` and changes nothing.

        The handler uses this only for failures proven to occur before the
        child adapter is invoked, so that invalid runtime-default
        configuration can be repaired and the turn explicitly retried
        without a duplicate model call. It is not a general unlock and never
        recovers an uncertain call.
        """
        return _run_with_retry(
            lambda: self._release_prelaunch_once(
                conversation_id=conversation_id,
                ownership_token=ownership_token,
                turn_id=turn_id,
                attempt_count=attempt_count,
            )
        )

    def _release_prelaunch_once(
        self,
        *,
        conversation_id: str,
        ownership_token: str,
        turn_id: str,
        attempt_count: int,
    ) -> ConversationRuntime:
        with self._database.session() as session:
            statement = (
                update(ConversationRuntime)
                .where(
                    ConversationRuntime.conversation_id
                    == conversation_id,
                    ConversationRuntime.reservation_token
                    == ownership_token,
                    ConversationRuntime.reservation_turn_id == turn_id,
                    ConversationRuntime.reservation_attempt_count
                    == attempt_count,
                    ConversationRuntime.blocked_reason.is_(None),
                    self._turn_running_exists(
                        conversation_id, turn_id, attempt_count
                    ),
                )
                .values(
                    reservation_token=None,
                    reservation_turn_id=None,
                    reservation_attempt_count=None,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            result = session.execute(statement)
            if result.rowcount != 1:
                session.expire_all()
                current = session.get(
                    ConversationRuntime, conversation_id
                )
                if current is None:
                    raise ConversationRuntimeNotFoundError(
                        conversation_id
                    )
                # Ownership mismatch or blocked binding: the narrow release
                # must never unlock anything it does not provably own.
                self._require_ownership(
                    current, ownership_token, turn_id, attempt_count
                )
                # Ownership is intact, so the conditional write failed only
                # because the reserved turn is no longer running.
                self._require_reserved_turn_running(
                    session, conversation_id, turn_id, attempt_count
                )
                raise ConversationRuntimeOwnershipError(  # pragma: no cover
                    conversation_id
                )

            session.expire_all()
            binding = session.get(ConversationRuntime, conversation_id)
            assert binding is not None  # pragma: no cover - row exists
            return binding

    # -- shared helpers --------------------------------------------------

    def _require_ownership(
        self,
        binding: ConversationRuntime,
        ownership_token: str,
        turn_id: str | None,
        attempt_count: int | None,
    ) -> None:
        """Raise typed errors unless the caller owns the reservation."""
        if binding.blocked_reason is not None:
            raise ConversationRuntimeBlockedError(
                binding.conversation_id, binding.blocked_reason
            )
        if binding.reservation_token != ownership_token:
            raise ConversationRuntimeOwnershipError(
                binding.conversation_id
            )
        if (
            turn_id is not None
            and binding.reservation_turn_id != turn_id
        ):
            raise ConversationRuntimeOwnershipError(
                binding.conversation_id
            )
        if (
            attempt_count is not None
            and binding.reservation_attempt_count != attempt_count
        ):
            raise ConversationRuntimeOwnershipError(
                binding.conversation_id
            )

    def _require_reserved_turn_running(
        self,
        session: Any,
        conversation_id: str,
        turn_id: str,
        attempt_count: int,
    ) -> Turn:
        """Raise unless the reserved turn is running at the reserved attempt."""
        turn = session.scalars(
            select(Turn).where(Turn.id == turn_id)
        ).one_or_none()
        if (
            turn is None
            or turn.conversation_id != conversation_id
            or turn.status != "running"
            or turn.attempt_count != attempt_count
        ):
            raise ConversationRuntimeTurnStateError(
                turn_id,
                None if turn is None else turn.status,
                None if turn is None else turn.attempt_count,
            )
        return turn
