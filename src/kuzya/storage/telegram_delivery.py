"""Durable Telegram delivery bookkeeping: message links and the outbox.

Two storage primitives live here:

* :class:`TelegramMessageLink` rows bind a Telegram ``(chat_id,
  telegram_message_id)`` pair to the conversation (and, when known, the backend
  message, turn and task) it belongs to. They are the single source of truth
  for routing an incoming Telegram Reply back into the original dialog.
* :class:`TelegramDelivery` rows are the durable outbox mirroring dialog
  messages into the owner's Telegram chat: a copy of every API question, every
  secretary reply for API-originated turns, and execution errors.

Both kinds of rows are written transactionally with the messages they refer to
via the ``note_*`` helpers, so persistence and delivery bookkeeping can never
diverge. Actual sending is at-least-once and owned by the unified Telegram
sender; this module only persists state transitions.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .database import Database
from .models import (
    Message,
    TelegramDelivery,
    TelegramMessageLink,
    utcnow,
)
from .operations import _is_sqlite_busy, _run_with_retry

#: Message input sources recognised by the delivery hooks.
INPUT_SOURCE_API = "api"
INPUT_SOURCE_TELEGRAM = "telegram"
INPUT_SOURCE_TASK_EVENT = "task_event"

#: Origins of linked Telegram messages: the user's own input vs. bot output.
MESSAGE_ORIGIN_USER = "user"
MESSAGE_ORIGIN_BOT = "bot"

#: Delivery row kinds.
DELIVERY_KIND_API_QUESTION = "api_question"
DELIVERY_KIND_REPLY = "reply"
DELIVERY_KIND_TURN_ERROR = "turn_error"

#: Delivery row statuses.
DELIVERY_STATUS_PENDING = "pending"
DELIVERY_STATUS_SENDING = "sending"
DELIVERY_STATUS_SENT = "sent"
DELIVERY_STATUS_FAILED = "failed"


def api_question_delivery_key(message_id: str) -> str:
    """Idempotency key for the Telegram copy of an API question."""
    return f"tg-q:{message_id}"


def reply_delivery_key(turn_id: str) -> str:
    """Idempotency key for the Telegram delivery of a turn's reply."""
    return f"tg-reply:{turn_id}"


def turn_error_delivery_key(turn_id: str) -> str:
    """Idempotency key for the Telegram delivery of a turn failure."""
    return f"tg-err:{turn_id}"


def record_message_link_in_session(
    session: Session,
    *,
    chat_id: int,
    telegram_message_id: int,
    conversation_id: str,
    message_id: str | None = None,
    turn_id: str | None = None,
    task_id: str | None = None,
    origin: str = MESSAGE_ORIGIN_USER,
) -> None:
    """Register a Telegram message link inside the caller's transaction.

    Re-registering the same ``(chat_id, telegram_message_id)`` is a no-op, so
    crash recovery and at-least-once senders cannot duplicate links.
    """
    session.execute(
        sqlite_insert(TelegramMessageLink)
        .values(
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            conversation_id=conversation_id,
            message_id=message_id,
            turn_id=turn_id,
            task_id=task_id,
            origin=origin,
        )
        .on_conflict_do_nothing(
            index_elements=[
                TelegramMessageLink.chat_id,
                TelegramMessageLink.telegram_message_id,
            ]
        )
    )


def enqueue_delivery_in_session(
    session: Session,
    *,
    conversation_id: str,
    kind: str,
    message_id: str | None = None,
    turn_id: str | None = None,
    idempotency_key: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> None:
    """Enqueue a delivery row inside the caller's transaction.

    An exact retry with the same ``idempotency_key`` inserts nothing. Keys are
    NULL for rows without a natural identity; SQLite treats NULL keys as
    distinct, matching the operations queue convention.
    """
    session.execute(
        sqlite_insert(TelegramDelivery)
        .values(
            id=uuid.uuid4().hex,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
            message_id=message_id,
            turn_id=turn_id,
            kind=kind,
            meta=dict(meta) if meta is not None else {},
        )
        .on_conflict_do_nothing(index_elements=[TelegramDelivery.idempotency_key])
    )


def note_appended_message(
    session: Session, *, message: Message, verified_caller: str | None = None
) -> None:
    """Hook for a freshly inserted message: links and question copies.

    Telegram-origin user input registers its message link (when the worker
    supplied ``chat_id``/``telegram_message_id`` metadata). API-origin user
    questions enqueue a Telegram copy carrying the server-verified caller.
    Other sources (``task_event`` events, ``secretary`` replies) are handled
    by their own paths and get nothing here.
    """
    if message.source == INPUT_SOURCE_TELEGRAM:
        chat_id = message.meta.get("chat_id")
        telegram_message_id = message.meta.get("telegram_message_id")
        if chat_id is None or telegram_message_id is None:
            return
        record_message_link_in_session(
            session,
            chat_id=int(chat_id),
            telegram_message_id=int(telegram_message_id),
            conversation_id=message.conversation_id,
            message_id=message.id,
            task_id=message.meta.get("task_id"),
            origin=MESSAGE_ORIGIN_USER,
        )
        return
    if message.source == INPUT_SOURCE_API and message.role == "user":
        meta: dict[str, Any] = {"input_source": INPUT_SOURCE_API}
        if verified_caller:
            meta["api_caller"] = verified_caller
        enqueue_delivery_in_session(
            session,
            conversation_id=message.conversation_id,
            kind=DELIVERY_KIND_API_QUESTION,
            message_id=message.id,
            idempotency_key=api_question_delivery_key(message.id),
            meta=meta,
        )


def note_completed_turn(session: Session, *, turn: Any, reply: Message) -> None:
    """Hook for a completed turn: enqueue delivery of the secretary reply.

    ``task_event`` turns are skipped: their replies are delivered by the task
    subscription machine, so a second outbox sender would duplicate them.
    Telegram- and API-originated turns get a row; the Telegram worker claims
    its row before sending, the outbox loop is the crash-recovery net.
    """
    input_message = session.scalars(
        select(Message).where(Message.id == turn.input_message_id)
    ).one_or_none()
    input_source = input_message.source if input_message is not None else None
    if input_source is None or input_source == INPUT_SOURCE_TASK_EVENT:
        return
    enqueue_delivery_in_session(
        session,
        conversation_id=turn.conversation_id,
        kind=DELIVERY_KIND_REPLY,
        message_id=reply.id,
        turn_id=turn.id,
        idempotency_key=reply_delivery_key(turn.id),
        meta={"input_source": input_source},
    )


def note_failed_turn(session: Session, *, turn: Any, error: str) -> None:
    """Hook for a failed turn: make the execution error visible in Telegram.

    Only API-originated turns enqueue an error row: the Telegram worker already
    reports its own failures inline, and task-event failures are reported by
    the subscription delivery.
    """
    input_message = session.scalars(
        select(Message).where(Message.id == turn.input_message_id)
    ).one_or_none()
    if input_message is None or input_message.source != INPUT_SOURCE_API:
        return
    enqueue_delivery_in_session(
        session,
        conversation_id=turn.conversation_id,
        turn_id=turn.id,
        kind=DELIVERY_KIND_TURN_ERROR,
        idempotency_key=turn_error_delivery_key(turn.id),
        meta={"input_source": INPUT_SOURCE_API, "error": error},
    )


class TelegramDeliveryRepository:
    """Advance and inspect the durable Telegram delivery state machine."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # -- links -----------------------------------------------------------

    def record_message_link(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        conversation_id: str,
        message_id: str | None = None,
        turn_id: str | None = None,
        task_id: str | None = None,
        origin: str = MESSAGE_ORIGIN_USER,
    ) -> None:
        """Register a Telegram message link in its own short transaction."""
        with self._database.session() as session:
            record_message_link_in_session(
                session,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                conversation_id=conversation_id,
                message_id=message_id,
                turn_id=turn_id,
                task_id=task_id,
                origin=origin,
            )

    def get_message_link(
        self, chat_id: int, telegram_message_id: int
    ) -> TelegramMessageLink | None:
        """Return the link for one Telegram message or ``None``."""
        with self._database.session() as session:
            return session.get(TelegramMessageLink, (chat_id, telegram_message_id))

    # -- delivery rows -----------------------------------------------------

    def get_delivery_by_idempotency_key(
        self, idempotency_key: str
    ) -> TelegramDelivery | None:
        """Return the delivery row stored under ``idempotency_key``."""
        with self._database.session() as session:
            return session.scalars(
                select(TelegramDelivery).where(
                    TelegramDelivery.idempotency_key == idempotency_key
                )
            ).one_or_none()

    def list_deliveries(
        self,
        *,
        conversation_id: str | None = None,
        status: str | None = None,
    ) -> list[TelegramDelivery]:
        """Return delivery rows ordered by sequence, oldest first."""
        with self._database.session() as session:
            statement = select(TelegramDelivery).order_by(TelegramDelivery.sequence)
            if conversation_id is not None:
                statement = statement.where(
                    TelegramDelivery.conversation_id == conversation_id
                )
            if status is not None:
                statement = statement.where(TelegramDelivery.status == status)
            return list(session.scalars(statement))

    def claim_delivery(self, delivery_id: str) -> bool:
        """Atomically claim a pending row (``pending → sending``).

        Returns ``False`` when the row is no longer pending, which is how the
        synchronous Telegram worker and the outbox loop avoid double sends.
        """
        with self._database.session() as session:
            return self._claim_in_session(session, delivery_id)

    def claim_next_pending(self) -> TelegramDelivery | None:
        """Claim the oldest pending row whose conversation is not in flight.

        A conversation with a row in ``sending`` is skipped entirely, so within
        one conversation rows always deliver in sequence order: an API question
        copy can never be sent after its reply. Conversations without an
        in-flight row proceed in parallel.
        """

        def _claim() -> TelegramDelivery | None:
            with self._database.session() as session:
                sending = set(
                    session.scalars(
                        select(TelegramDelivery.conversation_id).where(
                            TelegramDelivery.status == DELIVERY_STATUS_SENDING
                        )
                    )
                )
                statement = select(TelegramDelivery).where(
                    TelegramDelivery.status == DELIVERY_STATUS_PENDING
                )
                if sending:
                    statement = statement.where(
                        TelegramDelivery.conversation_id.not_in(sending)
                    )
                row = session.scalars(
                    statement.order_by(TelegramDelivery.sequence).limit(1)
                ).first()
                if row is None or not self._claim_in_session(session, row.id):
                    return None
                return session.scalars(
                    select(TelegramDelivery).where(TelegramDelivery.id == row.id)
                ).one()

        return _run_with_retry(_claim)

    def mark_part_sent(
        self, delivery_id: str, *, part_index: int, telegram_message_id: int
    ) -> None:
        """Persist progress after one part reached Telegram."""
        with self._database.session() as session:
            row = session.scalars(
                select(TelegramDelivery).where(TelegramDelivery.id == delivery_id)
            ).one_or_none()
            if row is None or row.status != DELIVERY_STATUS_SENDING:
                return
            ids = list(row.telegram_message_ids or [])
            ids.append(telegram_message_id)
            row.telegram_message_ids = ids
            row.parts_sent = part_index + 1
            row.updated_at = utcnow()

    def record_part_progress(
        self,
        delivery_id: str,
        *,
        part_index: int,
        chat_id: int,
        telegram_message_id: int,
        conversation_id: str,
        message_id: str | None = None,
        turn_id: str | None = None,
        task_id: str | None = None,
        origin: str = MESSAGE_ORIGIN_BOT,
    ) -> None:
        """Atomically persist part progress and its Telegram message link.

        The delivery progress update and the ``telegram_message_links`` insert
        commit in one transaction: a crash between them could otherwise mark a
        part delivered while leaving the message unresolvable for Reply
        routing (or vice versa).
        """
        with self._database.session() as session:
            row = session.scalars(
                select(TelegramDelivery).where(TelegramDelivery.id == delivery_id)
            ).one_or_none()
            if row is not None and row.status == DELIVERY_STATUS_SENDING:
                ids = list(row.telegram_message_ids or [])
                ids.append(telegram_message_id)
                row.telegram_message_ids = ids
                row.parts_sent = part_index + 1
                row.updated_at = utcnow()
            record_message_link_in_session(
                session,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                conversation_id=conversation_id,
                message_id=message_id,
                turn_id=turn_id,
                task_id=task_id,
                origin=origin,
            )

    def freeze_delivery_plate(self, delivery_id: str, plate: str | None) -> None:
        """Freeze the part-plan context plate on first use.

        Retries reuse the stored plate so the plan (and the meaning of the
        persisted ``parts_sent``) never changes between attempts, even if the
        user's active context — and therefore a freshly rendered plate —
        differs. A plate already stored is never overwritten.
        """
        with self._database.session() as session:
            row = session.scalars(
                select(TelegramDelivery).where(TelegramDelivery.id == delivery_id)
            ).one_or_none()
            if row is None or "plate" in (row.meta or {}):
                return
            meta = dict(row.meta or {})
            meta["plate"] = plate
            row.meta = meta
            row.updated_at = utcnow()

    def release_claim(self, delivery_id: str) -> None:
        """Return a claimed row to ``pending`` without delivering it.

        Used when no recipient is resolvable yet (the row waits for the owner's
        chat to become known) and after send errors with an unknown outcome.
        """
        with self._database.session() as session:
            session.execute(
                update(TelegramDelivery)
                .where(
                    TelegramDelivery.id == delivery_id,
                    TelegramDelivery.status == DELIVERY_STATUS_SENDING,
                )
                .values(
                    status=DELIVERY_STATUS_PENDING,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )

    def mark_sent(self, delivery_id: str) -> None:
        """Finalize a delivery after every part was sent."""
        with self._database.session() as session:
            session.execute(
                update(TelegramDelivery)
                .where(
                    TelegramDelivery.id == delivery_id,
                    TelegramDelivery.status == DELIVERY_STATUS_SENDING,
                )
                .values(
                    status=DELIVERY_STATUS_SENT,
                    sent_at=utcnow(),
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )

    def mark_failed(self, delivery_id: str, error: str) -> None:
        """Record a terminal delivery failure (diagnostics, row stays queryable)."""
        with self._database.session() as session:
            session.execute(
                update(TelegramDelivery)
                .where(
                    TelegramDelivery.id == delivery_id,
                    TelegramDelivery.status == DELIVERY_STATUS_SENDING,
                )
                .values(
                    status=DELIVERY_STATUS_FAILED,
                    error=error,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )

    def requeue_stale_sending(self, reason: str = "startup recovery") -> int:
        """Reopen every ``sending`` row as ``pending`` (startup recovery).

        A crash between claiming and sending leaves rows in ``sending``;
        replaying them is safe because only rows that never reached ``sent``
        are reopened. A part Telegram accepted just before the crash may be
        resent once — delivery is at-least-once by design.
        """
        with self._database.session() as session:
            result = session.execute(
                update(TelegramDelivery)
                .where(TelegramDelivery.status == DELIVERY_STATUS_SENDING)
                .values(
                    status=DELIVERY_STATUS_PENDING,
                    error=reason,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            return result.rowcount

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _claim_in_session(session: Session, delivery_id: str) -> bool:
        result = session.execute(
            update(TelegramDelivery)
            .where(
                TelegramDelivery.id == delivery_id,
                TelegramDelivery.status == DELIVERY_STATUS_PENDING,
            )
            .values(
                status=DELIVERY_STATUS_SENDING,
                attempt_count=TelegramDelivery.attempt_count + 1,
                claimed_at=utcnow(),
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1


__all__ = [
    "DELIVERY_KIND_API_QUESTION",
    "DELIVERY_KIND_REPLY",
    "DELIVERY_KIND_TURN_ERROR",
    "DELIVERY_STATUS_FAILED",
    "DELIVERY_STATUS_PENDING",
    "DELIVERY_STATUS_SENDING",
    "DELIVERY_STATUS_SENT",
    "INPUT_SOURCE_API",
    "INPUT_SOURCE_TASK_EVENT",
    "INPUT_SOURCE_TELEGRAM",
    "MESSAGE_ORIGIN_BOT",
    "MESSAGE_ORIGIN_USER",
    "TelegramDeliveryRepository",
    "api_question_delivery_key",
    "enqueue_delivery_in_session",
    "note_appended_message",
    "note_completed_turn",
    "note_failed_turn",
    "record_message_link_in_session",
    "reply_delivery_key",
    "turn_error_delivery_key",
]
