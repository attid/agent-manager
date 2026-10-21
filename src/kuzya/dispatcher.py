"""Background queue dispatcher for durable secretary turns.

The dispatcher is the continuous, fault-isolated execution loop around
:class:`~kuzya.secretary.TurnProcessor`. It claims one eligible turn
at a time, calls the injected handler outside every database transaction and
records either the reply or a full diagnostic failure on the turn.

Design boundaries
-----------------

* **Database is the queue.** Pending turns are discovered from SQLite; the
  dispatcher keeps no in-memory queue that could be lost on restart.
  :meth:`QueueDispatcher.step` delegates the atomic claim to
  :meth:`TurnProcessor.claim_next`, so concurrent dispatchers coordinate at
  SQLite level exactly like direct processor callers.
* **One worker owns one turn.** A claim carries a random ``claim_token``;
  completion and failure require that token, so a stale or crashed worker
  cannot finish a newer claim or append a duplicate reply.
* **Handler runs outside transactions.** The database is never held open
  across model or network work. A handler exception is captured on the failed
  turn and never escapes :meth:`step`.
* **The loop never dies from one bad turn.** Loop-level exceptions are logged
  at ``ERROR`` with a traceback and structured context, then the loop keeps
  polling. Only a stop request ends the loop.
* **Graceful stop.** ``stop()`` and a set stop event end the loop after the
  active turn finishes; a turn is never abandoned half-way by the dispatcher.
* **Startup reconciliation.** Before claiming any new work the dispatcher
  fails orphaned ``running`` turns left by a terminated process, so recovery
  matches the documented protocol in :meth:`TurnProcessor.reconcile_startup_orphans`.

Logging context
---------------

Every worker log record is emitted inside
``bind_log_context(worker_id=..., turn_id=..., conversation_id=...)``. Log
events use the stable ``event`` field: ``worker_start``, ``worker_reconciliation``,
``turn_claim``, ``turn_completed``, ``turn_failed``, ``worker_error`` and
``worker_stop``. This module never logs raw prompts, message text or
credentials.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import threading
import time
import traceback
import typing
import uuid
from collections.abc import Callable

from .logging import bind_log_context
from .secretary import (
    Handler,
    TurnInput,
    TurnInputMessageNotFoundError,
    TurnProcessor,
)
from .storage import ConversationRepository, Database, Message, Turn

logger = logging.getLogger(__name__)

#: A handler factory receives the claimed turn and returns a handler.
HandlerFactory = Callable[[Turn], Handler]

#: Accepted handler shapes.
HandlerLike = Handler | HandlerFactory


@dataclasses.dataclass(frozen=True)
class TurnOutcome:
    """Result of one dispatcher :meth:`QueueDispatcher.step`.

    ``status`` is ``"completed"`` when a reply was appended and the turn was
    marked completed, or ``"failed"`` when the turn was marked failed with a
    diagnostic error. ``duration_ms`` is the wall-clock handler time in
    milliseconds, measured without holding any database transaction.
    """

    status: str
    turn: Turn
    reply: Message | None = None
    error: str | None = None
    duration_ms: float = 0.0
    worker_id: str | None = None

    @property
    def turn_id(self) -> str:
        """Identifier of the processed turn."""
        return self.turn.id

    @property
    def conversation_id(self) -> str:
        """Conversation that owns the processed turn."""
        return self.turn.conversation_id


def _format_diagnostic(error: BaseException) -> str:
    """Render a handler failure as a durable, full traceback string."""

    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    if rendered:
        return rendered
    return f"{type(error).__name__}: {error}"


def _is_callable_annotation(annotation: object) -> bool:
    """Return true when ``annotation`` is a ``Callable`` type annotation."""

    if annotation is None:
        return False
    origin = typing.get_origin(annotation)
    return origin in (Callable, typing.Callable)  # type: ignore[comparison-overlap]


def _detect_handler_mode(handler: HandlerLike) -> str:
    """Best-effort classification of ``handler`` as ``direct``/``factory``.

    Returns ``"direct"`` for ``Callable[[TurnInput], str]``, ``"factory"`` for
    ``Callable[[Turn], Callable[[TurnInput], str]]`` and ``"auto"`` when the
    shape is ambiguous, for example an unannotated lambda. ``"auto"`` resolves
    the shape on the first call without failing the turn.
    """

    hints: dict[str, object] = {}
    try:
        hints = typing.get_type_hints(handler)
    except Exception:  # noqa: BLE001 - annotations are advisory only
        hints = {}

    return_annotation = hints.get("return")
    if _is_callable_annotation(return_annotation):
        return "factory"
    if return_annotation is str:
        return "direct"

    try:
        parameters = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return "auto"

    if parameters:
        parameter = parameters[0]
        annotation = hints.get(parameter.name, parameter.annotation)
        if annotation is Turn:
            return "factory"
        if annotation is TurnInput:
            return "direct"
    return "auto"


class QueueDispatcher:
    """Continuous, fault-isolated turn dispatcher over one database.

    Args:
        database: File-backed SQLite :class:`Database` shared with the API.
        handler: Either ``Callable[[TurnInput], str]`` or a factory
            ``Callable[[Turn], Callable[[TurnInput], str]]``. The factory shape
            is useful when the handler needs turn metadata (attempt count,
            conversation id) before producing the per-input callable.
        poll_interval_seconds: Idle wait between polls. Non-negative; ``0``
            yields the CPU between polls instead of busy-spinning.
        concurrency_limit: Number of worker loops ``run_loop`` starts. Each
            loop claims and executes at most one turn at a time; SQLite still
            allows only one writer per database file.
        worker_id: Optional stable identifier used in every log record. A
            random identifier is generated when omitted.

    The dispatcher never opens a database transaction around the handler and
    never raises from :meth:`step` or :meth:`run_loop` for a handler failure.
    """

    def __init__(
        self,
        database: Database,
        handler: HandlerLike,
        *,
        poll_interval_seconds: float = 0.5,
        concurrency_limit: int = 1,
        worker_id: str | None = None,
    ) -> None:
        if not callable(handler):
            raise TypeError("handler must be callable")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if concurrency_limit < 1:
            raise ValueError("concurrency_limit must be at least 1")

        self._database = database
        self._handler = handler
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._concurrency_limit = int(concurrency_limit)
        self.worker_id = worker_id or f"dispatcher-{uuid.uuid4().hex[:12]}"
        self._handler_mode = _detect_handler_mode(handler)

        self._processor = TurnProcessor(database)
        self._conversations = ConversationRepository(database)

        self._lifecycle_lock = threading.Lock()
        self._control_thread: threading.Thread | None = None
        self._worker_threads: list[threading.Thread] = []
        self._stop_event: threading.Event | None = None
        self._reconcile_lock = threading.Lock()
        self._reconciled = False

    # -- public API ------------------------------------------------------

    @property
    def processor(self) -> TurnProcessor:
        """The underlying :class:`TurnProcessor` (read-only convenience)."""

        return self._processor

    def step(self, conversation_id: str | None = None) -> TurnOutcome | None:
        """Atomically claim and execute at most one eligible turn.

        Returns ``None`` when no pending turn was available. Any exception
        raised while executing the turn is caught: the turn is marked
        ``failed`` with a full traceback, the failure is logged at ``ERROR``
        and the outcome is returned instead of being re-raised.
        """

        try:
            turn = self._processor.claim_next(
                conversation_id=conversation_id
            )
        except Exception as error:  # noqa: BLE001 - loop must survive
            self._log_loop_error("claim_next", error)
            return None

        if turn is None:
            return None

        with bind_log_context(
            worker_id=self.worker_id,
            turn_id=turn.id,
            conversation_id=turn.conversation_id,
        ):
            logger.info(
                "turn claimed",
                extra={
                    "event": "turn_claim",
                    "attempt_count": turn.attempt_count,
                },
            )
            started = time.perf_counter()
            claim_token = turn.claim_token or ""
            try:
                message = self._conversations.get_message(
                    turn.input_message_id
                )
                if message is None:  # pragma: no cover - FK makes this rare
                    raise TurnInputMessageNotFoundError(
                        turn.input_message_id
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
                reply_text = self._invoke_handler(turn, turn_input)
            except Exception as error:  # noqa: BLE001 - recorded as failure
                return self._record_failure(
                    turn, claim_token, error, started
                )

            try:
                reply = self._processor.complete(
                    turn.id, claim_token, text=str(reply_text)
                )
            except Exception as error:  # noqa: BLE001 - recorded as failure
                return self._record_failure(
                    turn, claim_token, error, started
                )

            duration_ms = (time.perf_counter() - started) * 1000.0
            logger.info(
                "turn completed duration_ms=%s reply_id=%s",
                round(duration_ms, 3),
                reply.id,
                extra={
                    "event": "turn_completed",
                    "duration_ms": round(duration_ms, 3),
                    "reply_id": reply.id,
                },
            )
            stored = self._processor.get_turn(turn.id) or turn
            return TurnOutcome(
                status="completed",
                turn=stored,
                reply=reply,
                duration_ms=duration_ms,
                worker_id=self.worker_id,
            )

    def start(self) -> None:
        """Start the background polling loop.

        Startup reconciliation runs synchronously before the loop thread is
        spawned, so an orphaned ``running`` turn is always resolved before
        this method returns. Calling :meth:`start` on an already running
        dispatcher is a no-op.
        """

        with self._lifecycle_lock:
            if (
                self._control_thread is not None
                and self._control_thread.is_alive()
            ):
                return
            self._stop_event = threading.Event()
            self._reconciled = False
            self._reconcile()
            self._control_thread = threading.Thread(
                target=self.run_loop,
                args=(self._stop_event,),
                name=f"queue-dispatcher-{self.worker_id}",
                daemon=True,
            )
            self._control_thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Request a graceful stop and wait for the active turn to finish.

        The stop event is set first; running workers finish the turn they are
        executing and then exit. ``timeout`` bounds the wait in seconds;
        ``None`` waits indefinitely.
        """

        event = self._stop_event
        if event is not None:
            try:
                event.set()
            except Exception:  # noqa: BLE001 - best-effort stop signal
                logger.exception("failed to set dispatcher stop event")
        thread = self._control_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)
        for worker in list(self._worker_threads):
            if worker is not threading.current_thread():
                worker.join(timeout)

    def run_loop(
        self, stop_event: threading.Event | None = None
    ) -> None:
        """Poll continuously until ``stop_event`` is set.

        The loop performs startup reconciliation, then starts
        ``concurrency_limit`` worker loops. Every worker catches its own
        loop-level errors; a single failure never terminates the process. A
        set stop event is only observed between turns, so the active turn is
        completed cleanly first. ``asyncio.Event`` values are also accepted.
        """

        if stop_event is None:
            stop_event = self._stop_event or threading.Event()
        self._stop_event = stop_event
        self._reconcile()
        self._log_worker_event("worker_start")

        try:
            if self._concurrency_limit == 1:
                self._worker_loop(stop_event, 0)
            else:
                workers = [
                    threading.Thread(
                        target=self._worker_loop,
                        args=(stop_event, index),
                        name=(
                            f"queue-dispatcher-{self.worker_id}-{index}"
                        ),
                        daemon=True,
                    )
                    for index in range(self._concurrency_limit)
                ]
                with self._lifecycle_lock:
                    self._worker_threads = workers
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()
        finally:
            self._log_worker_event("worker_stop")

    # -- internals -------------------------------------------------------

    def _worker_loop(
        self, stop_event: threading.Event, index: int
    ) -> None:
        """Poll and process turns until a stop is requested."""

        while not self._stop_requested(stop_event):
            try:
                outcome = self.step()
            except Exception as error:  # noqa: BLE001 - loop must survive
                self._log_loop_error("step", error, worker_index=index)
                outcome = None
            if outcome is None:
                self._idle_wait(stop_event)

    def _stop_requested(self, stop_event: threading.Event) -> bool:
        """Return true when the stop event has been signalled."""

        try:
            return bool(stop_event.is_set())
        except Exception:  # noqa: BLE001 - a broken event must not spin
            return False

    def _idle_wait(self, stop_event: threading.Event) -> None:
        """Wait for the next poll without busy-spinning."""

        if self._poll_interval_seconds <= 0:
            time.sleep(0)
            return
        if isinstance(stop_event, threading.Event):
            stop_event.wait(self._poll_interval_seconds)
            return
        if self._stop_requested(stop_event):
            return
        time.sleep(self._poll_interval_seconds)

    def _invoke_handler(
        self, turn: Turn, turn_input: TurnInput
    ) -> str:
        """Call the direct handler or factory shape for one turn."""

        if self._handler_mode == "factory":
            factory_handler = self._handler(turn)
            if not callable(factory_handler):
                raise TypeError(
                    "handler factory did not return a callable handler"
                )
            return factory_handler(turn_input)

        if self._handler_mode == "direct":
            return self._handler(turn_input)

        # Ambiguous shape: try the direct contract first, then fall back to
        # the factory contract if the handler clearly expected a Turn.
        try:
            produced = self._handler(turn_input)
        except AttributeError as direct_error:
            try:
                candidate = self._handler(turn)
            except Exception:  # noqa: BLE001 - keep the original failure
                raise direct_error
            if callable(candidate):
                return candidate(turn_input)
            raise direct_error
        if callable(produced):
            try:
                return produced(turn_input)
            except AttributeError:
                # The produced callable read an attribute that only the
                # claimed Turn exposes, so the factory was mis-called with a
                # TurnInput on the first attempt. Rebuild it with the Turn.
                candidate = self._handler(turn)
                if callable(candidate) and candidate is not produced:
                    return candidate(turn_input)
                raise
        return produced

    def _record_failure(
        self,
        turn: Turn,
        claim_token: str,
        error: BaseException,
        started: float,
    ) -> TurnOutcome:
        """Mark the claimed turn failed and log the diagnostic failure."""

        diagnostic = _format_diagnostic(error)
        duration_ms = (time.perf_counter() - started) * 1000.0
        failed_turn = turn
        try:
            failed_turn = self._processor.fail(
                turn.id, claim_token, diagnostic
            )
        except Exception as fail_error:  # noqa: BLE001 - loop must survive
            self._log_loop_error("fail", fail_error)
        logger.error(
            "turn failed duration_ms=%s error=%s",
            round(duration_ms, 3),
            f"{type(error).__name__}: {error}",
            exc_info=error,
            extra={
                "event": "turn_failed",
                "duration_ms": round(duration_ms, 3),
                "error": f"{type(error).__name__}: {error}",
            },
        )
        return TurnOutcome(
            status="failed",
            turn=failed_turn,
            error=diagnostic,
            duration_ms=duration_ms,
            worker_id=self.worker_id,
        )

    def _reconcile(self) -> None:
        """Run idempotent startup reconciliation once per dispatcher start."""

        with self._reconcile_lock:
            if self._reconciled:
                return
            self._reconciled = True
        try:
            result = self._processor.reconcile_startup_orphans()
        except Exception as error:  # noqa: BLE001 - loop must survive
            self._log_loop_error("reconcile_startup_orphans", error)
            return

        with bind_log_context(worker_id=self.worker_id):
            if result.turn_ids:
                logger.warning(
                    "dispatcher reconciled %d orphaned turn(s)",
                    len(result.turn_ids),
                    extra={
                        "event": "worker_reconciliation",
                        "orphaned_turns": len(result.turn_ids),
                        "turn_ids": list(result.turn_ids),
                        "conversation_ids": list(
                            result.conversation_ids
                        ),
                    },
                )
            else:
                logger.info(
                    "dispatcher startup reconciliation found no orphans",
                    extra={
                        "event": "worker_reconciliation",
                        "orphaned_turns": 0,
                    },
                )

    def _log_worker_event(
        self,
        event: str,
        level: int = logging.INFO,
        **fields: object,
    ) -> None:
        """Log a worker lifecycle event with the worker context bound."""

        with bind_log_context(worker_id=self.worker_id):
            logger.log(
                level,
                "%s",
                event,
                extra={"event": event, **fields},
            )

    def _log_loop_error(
        self,
        operation: str,
        error: BaseException,
        **fields: object,
    ) -> None:
        """Log an intercepted loop-level exception with its traceback."""

        with bind_log_context(worker_id=self.worker_id):
            logger.error(
                "dispatcher loop error operation=%s error=%s",
                operation,
                f"{type(error).__name__}: {error}",
                exc_info=error,
                extra={
                    "event": "worker_error",
                    "operation": operation,
                    "error": f"{type(error).__name__}: {error}",
                    **fields,
                },
            )


#: Backwards/compat alias for callers that prefer the generic name.
BackgroundWorker = QueueDispatcher

__all__ = [
    "BackgroundWorker",
    "HandlerFactory",
    "HandlerLike",
    "QueueDispatcher",
    "TurnOutcome",
]
