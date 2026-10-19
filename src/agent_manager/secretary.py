"""Durable one-turn secretary processor.

This module is a small, testable execution boundary around a single
assistant turn. It does not call a language model, does not talk to Telegram,
does not supervise processes and exposes no public worker-control HTTP
endpoints. A handler is injected as a plain callable that receives a
:class:`TurnInput` and returns reply text.

Persisted protocol
------------------

A :class:`~agent_manager.storage.Turn` links one existing inbound user
:class:`~agent_manager.storage.Message` to its conversation. The turn
``status`` is an execution-protocol value, not a task status:

* ``pending`` -- enqueued, not yet claimed.
* ``running`` -- claimed by exactly one worker, never more than one per
  conversation.
* ``completed`` -- one assistant reply was appended in the same transaction
  that marked the turn completed.
* ``failed`` -- the handler raised; the input and error are retained and no
  reply is fabricated. An explicit retry reopens the turn.

Ordering and concurrency
------------------------

Within a conversation, earlier ``pending`` and ``running`` turns block later
turns. Failed turns retain their diagnostics but do not stall new input.
An explicit retry is allowed only before any later turn has started.
Different conversations are independent and can proceed in parallel.

Claim and completion use short, atomic, conditional SQL updates. The handler
runs strictly outside any database transaction. Every claim carries a random
``claim_token``; ``complete``, ``fail`` and ``recover_abandoned`` require the
current token, so a stale token cannot finish or fail a newer claim or append
a reply.

Operations
----------

``enqueue``
    Validate that the input message exists, belongs to the conversation and
    has role ``user``; insert the turn idempotently on ``input_message_id``.
``claim_next``
    Atomically claim the next eligible pending turn (optionally scoped to one
    conversation) and return it with its claim token, or ``None``.
``complete``
    Append one assistant reply and mark the turn completed in one
    transaction. Repeated completion with the same token returns the original
    reply and appends nothing.
``fail``
    Mark the running turn failed with the handler error. No reply is written.
``retry_failed``
    Explicitly reopen a failed turn as ``pending`` so it can be claimed again.
    A startup-interruption block on the conversation is cleared in the same
    transaction, so a reconciled turn can acquire a fresh reservation. Other
    fail-closed blocks are left latched.
``recover_abandoned``
    Explicitly, and with caller authorization (the current claim token),
    return an abandoned ``running`` turn to ``pending``. There is no automatic
    timeout guess.
``reconcile_startup_orphans``
    Fail every ``running`` turn left behind by a terminated process, release
    its conversation reservation and record a diagnostic fail-closed block.
    Idempotent; there is no automatic model replay.
``run_once``
    Claim one turn, load its input, call the injected handler outside any
    transaction, then complete or fail the turn.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import exists, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased

from .logging import bind_log_context
from .storage import (
    Conversation,
    Database,
    Message,
    Turn,
    clear_interrupted_block,
    release_interrupted_reservation,
)
from .storage.models import utcnow
from .storage.repository import ConversationRepository

logger = logging.getLogger(__name__)

#: Maximum number of fresh claim attempts before giving up. Each attempt uses
#: a fresh connection, so a lost race never leaves a stale read snapshot.
_MAX_CLAIM_ATTEMPTS = 5

#: Source recorded on assistant replies produced by this processor.
REPLY_SOURCE = "secretary"


class TurnError(Exception):
    """Base class for turn processor errors."""


class TurnNotFoundError(TurnError):
    """Raised when a turn id does not exist."""

    def __init__(self, turn_id: str) -> None:
        super().__init__(f"turn not found: {turn_id}")
        self.turn_id = turn_id


class TurnInputMessageNotFoundError(TurnError):
    """Raised when the input message id does not exist."""

    def __init__(self, message_id: str) -> None:
        super().__init__(f"input message not found: {message_id}")
        self.message_id = message_id


class TurnConversationMismatchError(TurnError):
    """Raised when the input message belongs to a different conversation."""

    def __init__(self, message_id: str, expected: str, actual: str) -> None:
        super().__init__(
            f"input message {message_id!r} belongs to conversation "
            f"{actual!r}, not {expected!r}"
        )
        self.message_id = message_id
        self.expected_conversation_id = expected
        self.actual_conversation_id = actual


class TurnInputRoleError(TurnError):
    """Raised when the input message is not an inbound user message."""

    def __init__(self, message_id: str, role: str) -> None:
        super().__init__(
            f"input message {message_id!r} has role {role!r}, expected 'user'"
        )
        self.message_id = message_id
        self.role = role


class StaleClaimError(TurnError):
    """Raised when a caller no longer holds the turn's current claim token.

    This covers both an unknown turn (see :class:`TurnNotFoundError` for the
    explicit not-found case) and a turn that is running under a different
    token, is already completed, or is otherwise not in the required state.
    """

    def __init__(self, turn_id: str) -> None:
        super().__init__(
            f"claim token is missing, stale or not valid for turn {turn_id}"
        )
        self.turn_id = turn_id


class TurnStateError(TurnError):
    """Raised when a transition is attempted from an invalid turn state."""

    def __init__(self, turn_id: str, status: str, action: str) -> None:
        super().__init__(
            f"cannot {action} turn {turn_id} in status {status!r}"
        )
        self.turn_id = turn_id
        self.status = status
        self.action = action


@dataclasses.dataclass(frozen=True)
class TurnInput:
    """Immutable handler input for one claimed turn."""

    turn_id: str
    conversation_id: str
    input_message_id: str
    role: str
    text: str
    metadata: Mapping[str, Any]
    attempt_count: int


@dataclasses.dataclass(frozen=True)
class TurnEnqueueResult:
    """Outcome of one enqueue call.

    ``created`` is true when a new turn was written; false when the input
    message already had a turn and the stored turn was returned.
    """

    turn: Turn
    created: bool


@dataclasses.dataclass(frozen=True)
class RunOnceResult:
    """Outcome of :meth:`TurnProcessor.run_once`.

    ``status`` is ``"idle"`` when no eligible turn existed, ``"completed"``
    when a reply was appended, or ``"failed"`` when the handler raised.
    """

    status: str
    turn: Turn | None = None
    reply: Message | None = None
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class ReconciliationResult:
    """Outcome of :meth:`TurnProcessor.reconcile_startup_orphans`.

    ``turn_ids`` and ``conversation_ids`` contain one entry per orphaned
    ``running`` turn that was failed. Both lists are empty when a clean
    database or a repeated call reconciled nothing.
    """

    turn_ids: list[str]
    conversation_ids: list[str]


#: A handler receives the claimed input and returns reply text.
Handler = Callable[[TurnInput], str]


def _format_error(error: BaseException | str) -> str:
    """Render a handler failure for durable storage."""
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error)


class TurnProcessor:
    """Durable one-turn processor over a :class:`Database`.

    Each operation opens its own short transaction on its own connection. The
    processor is safe to instantiate once per worker process; independent
    instances over the same database coordinate purely at SQLite level.
    """

    def __init__(self, database: Database) -> None:
        self._database = database
        self._conversations = ConversationRepository(database)

    # -- reads -----------------------------------------------------------

    def get_turn(self, turn_id: str) -> Turn | None:
        """Return the turn with ``turn_id`` or ``None`` when absent."""
        with self._database.session() as session:
            return session.scalars(
                select(Turn).where(Turn.id == turn_id)
            ).one_or_none()

    # -- enqueue ---------------------------------------------------------

    def enqueue(
        self,
        *,
        conversation_id: str,
        input_message_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> TurnEnqueueResult:
        """Persist a pending turn for an existing inbound user message.

        The message must exist, must belong to ``conversation_id`` and must
        have role ``user``. ``input_message_id`` is unique, so replaying an
        enqueue returns the originally stored turn with ``created=False``
        instead of writing a second row. The insert is an atomic
        ``INSERT ... ON CONFLICT DO NOTHING``, which also makes concurrent
        duplicate enqueues safe.
        """
        with self._database.session() as session:
            message = session.scalars(
                select(Message).where(Message.id == input_message_id)
            ).one_or_none()
            if message is None:
                raise TurnInputMessageNotFoundError(input_message_id)
            if message.conversation_id != conversation_id:
                raise TurnConversationMismatchError(
                    input_message_id, conversation_id, message.conversation_id
                )
            if message.role != "user":
                raise TurnInputRoleError(input_message_id, message.role)

            turn_id = uuid.uuid4().hex
            statement = (
                sqlite_insert(Turn)
                .values(
                    id=turn_id,
                    conversation_id=conversation_id,
                    input_message_id=input_message_id,
                    status="pending",
                    attempt_count=0,
                    meta=dict(metadata) if metadata is not None else {},
                )
                .on_conflict_do_nothing(
                    index_elements=[Turn.input_message_id]
                )
            )
            result = session.execute(statement)
            if result.rowcount == 1:
                turn = session.scalars(
                    select(Turn).where(Turn.id == turn_id)
                ).one()
                return TurnEnqueueResult(turn=turn, created=True)

            existing = session.scalars(
                select(Turn).where(Turn.input_message_id == input_message_id)
            ).one()
            return TurnEnqueueResult(turn=existing, created=False)

    # -- claim -----------------------------------------------------------

    def claim_next(self, *, conversation_id: str | None = None) -> Turn | None:
        """Atomically claim the next eligible pending turn.

        A turn is eligible when it is ``pending`` and every earlier turn in
        the same conversation (by ``sequence``) is terminal. This keeps
        at most one running turn per conversation while allowing other
        conversations to proceed. ``conversation_id`` optionally restricts
        the search to one conversation.

        Returns the claimed :class:`Turn` with a fresh ``claim_token``, or
        ``None`` when nothing is eligible.
        """
        for _ in range(_MAX_CLAIM_ATTEMPTS):
            with self._database.session() as session:
                candidate = self._next_candidate(session, conversation_id)
                if candidate is None:
                    return None

                earlier = aliased(Turn)
                blocked = exists().where(
                    earlier.conversation_id == Turn.conversation_id,
                    earlier.sequence < Turn.sequence,
                    earlier.status.in_(("pending", "running")),
                )
                claim_token = uuid.uuid4().hex
                result = session.execute(
                    update(Turn)
                    .where(Turn.id == candidate.id, Turn.status == "pending", ~blocked)
                    .values(
                        status="running",
                        claim_token=claim_token,
                        claimed_at=utcnow(),
                        updated_at=utcnow(),
                        attempt_count=Turn.attempt_count + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount == 1:
                    # The conditional update bypassed the identity map, so
                    # refresh the claimed row to expose the new token/status.
                    session.refresh(candidate)
                    return candidate
            # Lost a race against another worker; retry on a fresh snapshot.
        return None

    @staticmethod
    def _next_candidate(
        session: Session, conversation_id: str | None
    ) -> Turn | None:
        earlier = aliased(Turn)
        blocked = exists().where(
            earlier.conversation_id == Turn.conversation_id,
            earlier.sequence < Turn.sequence,
            earlier.status.in_(("pending", "running")),
        )
        statement = (
            select(Turn)
            .where(Turn.status == "pending", ~blocked)
            .order_by(Turn.sequence)
            .limit(1)
        )
        if conversation_id is not None:
            statement = statement.where(Turn.conversation_id == conversation_id)
        return session.scalars(statement).first()

    # -- complete / fail -------------------------------------------------

    def complete(
        self,
        turn_id: str,
        claim_token: str,
        *,
        text: str,
        source: str = REPLY_SOURCE,
        metadata: Mapping[str, Any] | None = None,
    ) -> Message:
        """Append one assistant reply and complete the turn atomically.

        The conditional update requires the turn to still be ``running`` under
        ``claim_token``. Only then is the assistant message inserted and the
        conversation ``updated_at`` advanced, all in one transaction.

        Repeated completion with the same token does not append a second
        reply: the original assistant message is returned. A different or
        stale token raises :class:`StaleClaimError` and appends nothing.
        """
        with self._database.session() as session:
            turn = session.scalars(
                select(Turn).where(Turn.id == turn_id)
            ).one_or_none()
            if turn is None:
                raise TurnNotFoundError(turn_id)

            if turn.status == "completed":
                if (
                    turn.claim_token == claim_token
                    and turn.reply_message_id is not None
                ):
                    existing = session.scalars(
                        select(Message).where(
                            Message.id == turn.reply_message_id
                        )
                    ).one_or_none()
                    if existing is not None:
                        return existing
                raise StaleClaimError(turn_id)

            if turn.status != "running" or turn.claim_token != claim_token:
                raise StaleClaimError(turn_id)

            reply_id = uuid.uuid4().hex
            # Fence first: only the worker holding the current token may flip
            # the turn to completed. The reply row is inserted and linked in
            # the same transaction, so the FK and atomicity both hold.
            result = session.execute(
                update(Turn)
                .where(
                    Turn.id == turn_id,
                    Turn.status == "running",
                    Turn.claim_token == claim_token,
                )
                .values(
                    status="completed",
                    error=None,
                    finished_at=utcnow(),
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise StaleClaimError(turn_id)

            reply = Message(
                id=reply_id,
                conversation_id=turn.conversation_id,
                role="assistant",
                text=text,
                source=source,
                external_id=turn.id,
                meta=dict(metadata) if metadata is not None else {},
            )
            session.add(reply)
            session.flush()
            session.execute(
                update(Turn)
                .where(Turn.id == turn_id)
                .values(reply_message_id=reply_id, updated_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            session.execute(
                update(Conversation)
                .where(Conversation.id == turn.conversation_id)
                .values(updated_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            return reply

    def fail(self, turn_id: str, claim_token: str, error: str) -> Turn:
        """Mark the running turn failed, retaining the error and no reply.

        Requires the current ``claim_token``. A stale or different token
        raises :class:`StaleClaimError` and the turn is left untouched.
        """
        with self._database.session() as session:
            result = session.execute(
                update(Turn)
                .where(
                    Turn.id == turn_id,
                    Turn.status == "running",
                    Turn.claim_token == claim_token,
                )
                .values(
                    status="failed",
                    error=error,
                    finished_at=utcnow(),
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                still_exists = session.scalars(
                    select(Turn.id).where(Turn.id == turn_id)
                ).one_or_none()
                if still_exists is None:
                    raise TurnNotFoundError(turn_id)
                raise StaleClaimError(turn_id)
            return session.scalars(
                select(Turn).where(Turn.id == turn_id)
            ).one()

    # -- explicit retry / recovery --------------------------------------

    def retry_failed(self, turn_id: str) -> Turn:
        """Explicitly reopen a failed turn as ``pending``.

        Retrying is refused once any later turn has started, preserving
        execution order even when retry races a new claim.

        The previous error and claim token are cleared; the attempt count and
        timestamps are retained as history. The transition is a single atomic
        compare-and-set (``failed -> pending``), so a concurrent retry or
        claim that already advanced the turn cannot be overwritten: if the
        row is no longer ``failed`` the call raises
        :class:`TurnStateError`. A missing turn raises
        :class:`TurnNotFoundError`.

        When the turn was failed by startup reconciliation, the
        conversation's interruption block is cleared in the same transaction
        so the reopened turn can acquire a new reservation. A fail-closed
        block recorded for an uncertain model call is never cleared here.
        """
        later = aliased(Turn)
        later_started = exists().where(
            later.conversation_id == Turn.conversation_id,
            later.sequence > Turn.sequence,
            later.attempt_count > 0,
        )
        with self._database.session() as session:
            result = session.execute(
                update(Turn)
                .where(Turn.id == turn_id, Turn.status == "failed", ~later_started)
                .values(
                    status="pending",
                    claim_token=None,
                    error=None,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                current = session.scalars(
                    select(Turn).where(Turn.id == turn_id)
                ).one_or_none()
                if current is None:
                    raise TurnNotFoundError(turn_id)
                raise TurnStateError(
                    turn_id, current.status,
                    "retry after later work started" if current.status == "failed" else "retry",
                )
            reopened = session.scalars(
                select(Turn).where(Turn.id == turn_id)
            ).one()
            clear_interrupted_block(
                session, conversation_id=reopened.conversation_id
            )
            return reopened

    def recover_abandoned(self, turn_id: str, *, claim_token: str) -> Turn:
        """Explicitly reopen an abandoned ``running`` turn as ``pending``.

        Recovery is caller-authorized: the caller must present the current
        ``claim_token``. There is no automatic timeout or stale-claim guess.
        A missing turn raises :class:`TurnNotFoundError`; any token or state
        mismatch raises :class:`StaleClaimError`.
        """
        with self._database.session() as session:
            result = session.execute(
                update(Turn)
                .where(
                    Turn.id == turn_id,
                    Turn.status == "running",
                    Turn.claim_token == claim_token,
                )
                .values(
                    status="pending",
                    claim_token=None,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                still_exists = session.scalars(
                    select(Turn.id).where(Turn.id == turn_id)
                ).one_or_none()
                if still_exists is None:
                    raise TurnNotFoundError(turn_id)
                raise StaleClaimError(turn_id)
            return session.scalars(
                select(Turn).where(Turn.id == turn_id)
            ).one()

    # -- startup reconciliation ------------------------------------------

    def reconcile_startup_orphans(
        self,
        reason: str = "Turn execution was interrupted by process restart",
    ) -> ReconciliationResult:
        """Fail orphaned ``running`` turns and release their reservations.

        After a process restart, a turn left ``running`` has no live worker.
        Every such turn is moved to ``failed`` with ``reason`` and a
        ``finished_at`` timestamp, and its claim token is cleared. When the
        conversation's runtime still holds an active reservation for that
        turn, the reservation is cleared and a fail-closed ``blocked_reason``
        beginning with ``"Interrupted: "`` is recorded. The turn transition
        and the reservation release commit in one short transaction per turn,
        so a crash cannot leave a failed turn with a hung reservation.

        The operation is idempotent: a repeated call or a clean database
        reconciles nothing and returns empty lists. Each reconciled turn is
        logged at ``WARNING`` with its ``turn_id``, ``conversation_id`` and
        ``attempt_count``.
        """
        message = str(reason)
        with self._database.session() as session:
            candidates = [
                (turn.id, turn.conversation_id, turn.attempt_count)
                for turn in session.scalars(
                    select(Turn).where(Turn.status == "running")
                ).all()
            ]

        reconciled_turns: list[str] = []
        reconciled_conversations: list[str] = []
        for turn_id, conversation_id, attempt_count in candidates:
            with self._database.session() as session:
                result = session.execute(
                    update(Turn)
                    .where(Turn.id == turn_id, Turn.status == "running")
                    .values(
                        status="failed",
                        error=message,
                        claim_token=None,
                        finished_at=utcnow(),
                        updated_at=utcnow(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    # A live worker completed or recovered the turn first;
                    # leave the committed outcome untouched.
                    continue
                release_interrupted_reservation(
                    session,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    reason=message,
                )

            reconciled_turns.append(turn_id)
            reconciled_conversations.append(conversation_id)
            with bind_log_context(
                turn_id=turn_id, conversation_id=conversation_id
            ):
                logger.warning(
                    "reconciled orphaned running turn attempt_count=%s",
                    attempt_count,
                    extra={
                        "event": "turn_reconciliation",
                        "turn_id": turn_id,
                        "conversation_id": conversation_id,
                        "attempt_count": attempt_count,
                    },
                )

        return ReconciliationResult(
            turn_ids=reconciled_turns,
            conversation_ids=reconciled_conversations,
        )

    # -- one-pass runner -------------------------------------------------

    def run_once(
        self,
        handler: Handler,
        *,
        conversation_id: str | None = None,
    ) -> RunOnceResult:
        """Claim one turn, run ``handler`` outside the DB, record the result.

        The handler receives a :class:`TurnInput` and returns reply text. It is
        invoked after the claim transaction has committed and before the
        completion transaction starts, so the database is never held open
        across model or network work. A handler exception is captured on the
        failed turn and returned with that turn's stored state; it is not
        re-raised.
        """
        turn = self.claim_next(conversation_id=conversation_id)
        if turn is None:
            return RunOnceResult(status="idle")

        message = self._conversations.get_message(turn.input_message_id)
        if message is None:  # pragma: no cover - FK makes this unreachable
            error = f"input message not found: {turn.input_message_id}"
            failed_turn = self.fail(turn.id, turn.claim_token or "", error)
            return RunOnceResult(
                status="failed", turn=failed_turn, error=error
            )

        turn_input = TurnInput(
            turn_id=turn.id,
            conversation_id=turn.conversation_id,
            input_message_id=turn.input_message_id,
            role=message.role,
            text=message.text,
            metadata=dict(message.meta or {}),
            attempt_count=turn.attempt_count,
        )
        try:
            reply_text = handler(turn_input)
        except Exception as error:  # noqa: BLE001 - recorded, not swallowed
            formatted = _format_error(error)
            failed_turn = self.fail(
                turn.id, turn.claim_token or "", formatted
            )
            return RunOnceResult(
                status="failed", turn=failed_turn, error=formatted
            )

        reply = self.complete(
            turn.id, turn.claim_token or "", text=str(reply_text)
        )
        return RunOnceResult(
            status="completed",
            turn=self.get_turn(turn.id),
            reply=reply,
        )
