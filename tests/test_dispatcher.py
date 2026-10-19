"""Acceptance tests for the background queue dispatcher.

Every test runs against a real migrated temporary file database, the real
``TurnProcessor`` and real SQLite connections. No model, network or external
service is involved. The dispatcher is exercised through its public surface:
``step``, ``run_loop``, ``start`` and ``stop``.
"""

from __future__ import annotations

import logging
import pathlib
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

from sqlalchemy import select

from agent_manager.dispatcher import QueueDispatcher, TurnOutcome
from agent_manager.secretary import TurnInput, TurnProcessor
from agent_manager.storage import (
    ConversationRepository,
    Database,
    Turn,
)


def _database(migrated_db: pathlib.Path) -> Database:
    return Database.from_path(migrated_db)


def _conversation(
    database: Database, conversation_id: str
) -> ConversationRepository:
    repository = ConversationRepository(database)
    if repository.get_conversation(conversation_id) is None:
        repository.create_conversation(id=conversation_id)
    return repository


def _enqueue(database: Database, conversation_id: str, text: str) -> Turn:
    repository = _conversation(database, conversation_id)
    message = repository.append_message(
        conversation_id=conversation_id,
        role="user",
        text=text,
        source="test",
    ).message
    return TurnProcessor(database).enqueue(
        conversation_id=conversation_id,
        input_message_id=message.id,
    ).turn


def _turn_status(database: Database, turn_id: str) -> str | None:
    turn = TurnProcessor(database).get_turn(turn_id)
    return None if turn is None else turn.status


def _stored_turns(database: Database, conversation_id: str) -> list[Turn]:
    with database.session() as session:
        return list(
            session.scalars(
                select(Turn)
                .where(Turn.conversation_id == conversation_id)
                .order_by(Turn.sequence)
            ).all()
        )


def _replies(database: Database, conversation_id: str) -> list[str]:
    return [
        message.text
        for message in ConversationRepository(database).list_messages(
            conversation_id
        )
        if message.role == "assistant"
    ]


def _orphan_running_turn(
    database: Database,
    *,
    conversation_id: str = "conv-orphan",
    text: str = "orphan input",
) -> Turn:
    turn = _enqueue(database, conversation_id, text)
    claimed = TurnProcessor(database).claim_next(
        conversation_id=conversation_id
    )
    assert claimed is not None
    return claimed


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
    interval: float = 0.01,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# -- draining ------------------------------------------------------------


def test_step_returns_none_for_an_empty_queue(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        dispatcher = QueueDispatcher(database, lambda turn_input: "reply")
        assert dispatcher.step() is None
    finally:
        database.dispose()


def test_step_drains_sequential_turns_across_conversations(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        _enqueue(database, "conv-a", "a-1")
        _enqueue(database, "conv-a", "a-2")
        _enqueue(database, "conv-b", "b-1")

        dispatcher = QueueDispatcher(
            database, lambda turn_input: f"echo:{turn_input.text}"
        )

        outcomes: list[TurnOutcome] = []
        while True:
            outcome = dispatcher.step()
            if outcome is None:
                break
            outcomes.append(outcome)

        assert [outcome.status for outcome in outcomes] == [
            "completed",
            "completed",
            "completed",
        ]
        assert {outcome.conversation_id for outcome in outcomes} == {
            "conv-a",
            "conv-b",
        }
        for conversation_id, expected in (
            ("conv-a", ["echo:a-1", "echo:a-2"]),
            ("conv-b", ["echo:b-1"]),
        ):
            turns = _stored_turns(database, conversation_id)
            assert [turn.status for turn in turns] == ["completed"] * len(
                expected
            )
            assert _replies(database, conversation_id) == expected
    finally:
        database.dispose()


def test_run_loop_drains_multi_turn_queue_and_stops_cleanly(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        for conversation_id in ("conv-a", "conv-b", "conv-c"):
            for index in range(3):
                _enqueue(database, conversation_id, f"{conversation_id}-{index}")

        dispatcher = QueueDispatcher(
            database,
            lambda turn_input: f"echo:{turn_input.text}",
            poll_interval_seconds=0.01,
        )
        stop_event = threading.Event()
        thread = threading.Thread(
            target=dispatcher.run_loop,
            args=(stop_event,),
            daemon=True,
        )
        thread.start()

        def drained() -> bool:
            return all(
                turn.status == "completed"
                for conversation_id in ("conv-a", "conv-b", "conv-c")
                for turn in _stored_turns(database, conversation_id)
            )

        assert _wait_until(drained)
        stop_event.set()
        thread.join(timeout=5)
        assert not thread.is_alive()

        for conversation_id in ("conv-a", "conv-b", "conv-c"):
            replies = _replies(database, conversation_id)
            assert sorted(replies) == sorted(
                f"echo:{conversation_id}-{index}" for index in range(3)
            )
    finally:
        database.dispose()


def test_factory_handler_shape_receives_the_claimed_turn(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        _enqueue(database, "conv-factory", "ping")

        def factory(turn: Turn) -> Callable[[TurnInput], str]:
            return lambda turn_input: (
                f"factory:{turn.conversation_id}:{turn_input.text}"
            )

        dispatcher = QueueDispatcher(database, factory)
        outcome = dispatcher.step()

        assert outcome is not None
        assert outcome.status == "completed"
        assert _replies(database, "conv-factory") == [
            "factory:conv-factory:ping"
        ]
    finally:
        database.dispose()


def test_unannotated_factory_handler_uses_the_claimed_turn(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        _enqueue(database, "conv-ambig", "ping")

        def factory(turn):  # noqa: ANN001, ANN202 - ambiguous on purpose
            return lambda turn_input: f"{turn.status}:{turn_input.text}"

        dispatcher = QueueDispatcher(database, factory)
        outcome = dispatcher.step()

        assert outcome is not None
        assert outcome.status == "completed"
        assert _replies(database, "conv-ambig") == ["running:ping"]
    finally:
        database.dispose()


# -- fault isolation -----------------------------------------------------


def test_handler_exception_fails_turn_logs_traceback_and_continues(
    migrated_db: pathlib.Path,
    caplog,
) -> None:
    database = _database(migrated_db)
    try:
        bad_turn = _enqueue(database, "conv-bad", "bad input")
        _enqueue(database, "conv-good", "good input")

        def handler(turn_input: TurnInput) -> str:
            if turn_input.conversation_id == "conv-bad":
                raise RuntimeError("handler exploded")
            return f"echo:{turn_input.text}"

        dispatcher = QueueDispatcher(database, handler)

        with caplog.at_level(
            logging.ERROR, logger="agent_manager.dispatcher"
        ):
            failed = dispatcher.step()
            succeeded = dispatcher.step()
            idle = dispatcher.step()

        assert failed is not None
        assert failed.status == "failed"
        assert failed.turn_id == bad_turn.id
        assert failed.error is not None
        assert "Traceback" in failed.error
        assert "RuntimeError: handler exploded" in failed.error

        stored_bad = TurnProcessor(database).get_turn(bad_turn.id)
        assert stored_bad is not None
        assert stored_bad.status == "failed"
        assert stored_bad.error == failed.error
        assert stored_bad.reply_message_id is None
        assert _replies(database, "conv-bad") == []

        assert succeeded is not None
        assert succeeded.status == "completed"
        assert _replies(database, "conv-good") == ["echo:good input"]
        assert idle is None

        failure_records = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "turn_failed"
        ]
        assert failure_records
        assert failure_records[0].exc_info is not None
        assert failure_records[0].worker_id == dispatcher.worker_id
        assert "Traceback" in logging.Formatter().formatException(
            failure_records[0].exc_info
        )
    finally:
        database.dispose()


def test_loop_level_error_is_logged_and_loop_survives(
    migrated_db: pathlib.Path,
    caplog,
) -> None:
    database = _database(migrated_db)
    try:
        dispatcher = QueueDispatcher(
            database,
            lambda turn_input: "reply",
            poll_interval_seconds=0.01,
        )
        attempts = {"count": 0}

        def broken_step(conversation_id: str | None = None) -> None:
            attempts["count"] += 1
            raise RuntimeError("loop boom")

        dispatcher.step = broken_step  # type: ignore[method-assign]

        stop_event = threading.Event()
        with caplog.at_level(
            logging.ERROR, logger="agent_manager.dispatcher"
        ):
            thread = threading.Thread(
                target=dispatcher.run_loop,
                args=(stop_event,),
                daemon=True,
            )
            thread.start()
            assert _wait_until(lambda: attempts["count"] >= 2)
            stop_event.set()
            thread.join(timeout=5)

        assert not thread.is_alive()
        assert attempts["count"] >= 2
        error_records = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "worker_error"
        ]
        assert error_records
        assert error_records[0].worker_id == dispatcher.worker_id
        assert error_records[0].exc_info is not None
    finally:
        database.dispose()


# -- idle polling --------------------------------------------------------


def test_idle_polling_does_not_busy_spin_or_leak(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        dispatcher = QueueDispatcher(
            database,
            lambda turn_input: "reply",
            poll_interval_seconds=0.05,
        )
        assert dispatcher.step() is None

        claim_calls: list[int] = []
        original_claim = dispatcher.processor.claim_next

        def counting_claim(*args, **kwargs):
            claim_calls.append(1)
            return original_claim(*args, **kwargs)

        dispatcher.processor.claim_next = counting_claim  # type: ignore[method-assign]

        active_sessions = {"count": 0}
        real_session = database.session

        @contextmanager
        def tracking_session():
            active_sessions["count"] += 1
            try:
                with real_session() as session:
                    yield session
            finally:
                active_sessions["count"] -= 1

        database.session = tracking_session  # type: ignore[method-assign]

        stop_event = threading.Event()
        thread = threading.Thread(
            target=dispatcher.run_loop,
            args=(stop_event,),
            daemon=True,
        )
        thread.start()
        time.sleep(0.25)
        stop_event.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        # A busy loop would perform thousands of claims in 250 ms; a polite
        # 50 ms poll performs only a handful.
        assert 1 <= len(claim_calls) <= 15
        # Every session opened by an idle poll was also closed: no connection
        # (and therefore no transaction or file handle) is retained.
        assert active_sessions["count"] == 0
    finally:
        database.dispose()


# -- graceful stop -------------------------------------------------------


def test_stop_event_exits_run_loop_cleanly(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        dispatcher = QueueDispatcher(
            database,
            lambda turn_input: "reply",
            poll_interval_seconds=0.01,
        )
        stop_event = threading.Event()
        stop_event.set()
        dispatcher.run_loop(stop_event)
    finally:
        database.dispose()


def test_start_stop_completes_active_turn_before_shutdown(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        turn = _enqueue(database, "conv-active", "long work")
        entered = threading.Event()
        release = threading.Event()

        def handler(turn_input: TurnInput) -> str:
            entered.set()
            assert release.wait(timeout=5)
            return "finished"

        dispatcher = QueueDispatcher(
            database, handler, poll_interval_seconds=0.01
        )
        dispatcher.start()
        assert entered.wait(timeout=5)

        stopper = threading.Thread(
            target=dispatcher.stop, kwargs={"timeout": 5}, daemon=True
        )
        stopper.start()
        time.sleep(0.05)
        release.set()
        stopper.join(timeout=5)

        assert not stopper.is_alive()
        stored = TurnProcessor(database).get_turn(turn.id)
        assert stored is not None
        assert stored.status == "completed"
        assert _replies(database, "conv-active") == ["finished"]
    finally:
        database.dispose()


def test_stop_before_start_is_a_noop(migrated_db: pathlib.Path) -> None:
    database = _database(migrated_db)
    try:
        dispatcher = QueueDispatcher(database, lambda turn_input: "reply")
        dispatcher.stop(timeout=1)
    finally:
        database.dispose()


# -- startup reconciliation ---------------------------------------------


def test_start_reconciles_orphans_before_claiming_new_work(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        orphan = _orphan_running_turn(database)
        fresh = _enqueue(database, "conv-fresh", "fresh input")
        observed: dict[str, str | None] = {}

        def handler(turn_input: TurnInput) -> str:
            observed["orphan_status"] = _turn_status(database, orphan.id)
            return f"echo:{turn_input.text}"

        dispatcher = QueueDispatcher(
            database, handler, poll_interval_seconds=0.01
        )
        dispatcher.start()

        assert _wait_until(
            lambda: _turn_status(database, fresh.id) == "completed"
        )
        dispatcher.stop(timeout=5)

        stored_orphan = TurnProcessor(database).get_turn(orphan.id)
        assert stored_orphan is not None
        assert stored_orphan.status == "failed"
        assert stored_orphan.error is not None
        assert "interrupted" in stored_orphan.error.lower()
        assert stored_orphan.claim_token is None
        assert observed["orphan_status"] == "failed"

        stored_fresh = TurnProcessor(database).get_turn(fresh.id)
        assert stored_fresh is not None
        assert stored_fresh.status == "completed"
        assert _replies(database, "conv-fresh") == ["echo:fresh input"]
    finally:
        database.dispose()


def test_run_loop_reconciles_before_processing(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        orphan = _orphan_running_turn(
            database, conversation_id="conv-orphan-2"
        )
        dispatcher = QueueDispatcher(
            database,
            lambda turn_input: "reply",
            poll_interval_seconds=0.01,
        )
        stop_event = threading.Event()
        thread = threading.Thread(
            target=dispatcher.run_loop,
            args=(stop_event,),
            daemon=True,
        )
        thread.start()
        assert _wait_until(
            lambda: _turn_status(database, orphan.id) == "failed"
        )
        stop_event.set()
        thread.join(timeout=5)
        assert not thread.is_alive()

        stored = TurnProcessor(database).get_turn(orphan.id)
        assert stored is not None
        assert stored.status == "failed"
    finally:
        database.dispose()
