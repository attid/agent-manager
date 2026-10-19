"""Acceptance tests for the durable one-turn secretary processor.

Every test runs against a real migrated temporary file database and the real
migration chain, never an in-memory database. Handlers are deterministic
plain callables, so no model, network or external service is involved.
"""

from __future__ import annotations

import pathlib
import threading

import pytest
from alembic import command
from sqlalchemy import event, inspect, select

from agent_manager.secretary import (
    StaleClaimError,
    TurnConversationMismatchError,
    TurnInputMessageNotFoundError,
    TurnInputRoleError,
    TurnNotFoundError,
    TurnProcessor,
    TurnStateError,
)
from agent_manager.storage import (
    ConversationRepository,
    Database,
    TaskRepository,
    Turn,
)

from .conftest import alembic_config


def _repository(database: Database, conversation_id: str = "conv-1"):
    repository = ConversationRepository(database)
    if repository.get_conversation(conversation_id) is None:
        repository.create_conversation(id=conversation_id)
    return repository


def _message(
    database: Database,
    *,
    conversation_id: str = "conv-1",
    text: str = "hello",
    role: str = "user",
):
    repository = _repository(database, conversation_id)
    return repository.append_message(
        conversation_id=conversation_id,
        role=role,
        text=text,
        source="test",
    ).message


def _enqueue(
    database: Database,
    *,
    conversation_id: str = "conv-1",
    text: str = "hello",
    role: str = "user",
    processor: TurnProcessor | None = None,
):
    message = _message(
        database, conversation_id=conversation_id, text=text, role=role
    )
    active = processor or TurnProcessor(database)
    return active.enqueue(
        conversation_id=conversation_id, input_message_id=message.id
    )


# -- enqueue validation and idempotency ---------------------------------


def test_duplicate_enqueue_is_idempotent(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        message = _message(database)
        processor = TurnProcessor(database)

        first = processor.enqueue(
            conversation_id="conv-1", input_message_id=message.id
        )
        second = processor.enqueue(
            conversation_id="conv-1", input_message_id=message.id
        )

        assert first.created is True
        assert second.created is False
        assert first.turn.id == second.turn.id
        assert first.turn.attempt_count == 0

        claimed = processor.claim_next(conversation_id="conv-1")
        assert claimed is not None
        assert claimed.id == first.turn.id
        # A duplicate enqueue cannot have created a second pending turn.
        assert processor.claim_next(conversation_id="conv-1") is None
    finally:
        database.dispose()


def test_enqueue_rejects_unknown_message(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        with pytest.raises(TurnInputMessageNotFoundError):
            TurnProcessor(database).enqueue(
                conversation_id="conv-1", input_message_id="absent"
            )
    finally:
        database.dispose()


def test_enqueue_rejects_wrong_conversation(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        _repository(database, "conv-1")
        _repository(database, "conv-2")
        message = _message(database, conversation_id="conv-1")

        with pytest.raises(TurnConversationMismatchError):
            TurnProcessor(database).enqueue(
                conversation_id="conv-2", input_message_id=message.id
            )
    finally:
        database.dispose()


def test_enqueue_rejects_non_user_message(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        # Storage still accepts other roles; only enqueue is role-restricted.
        message = _message(database, role="assistant")

        with pytest.raises(TurnInputRoleError):
            TurnProcessor(database).enqueue(
                conversation_id="conv-1", input_message_id=message.id
            )

        stored = ConversationRepository(database).get_message(message.id)
        assert stored is not None
        assert stored.role == "assistant"
    finally:
        database.dispose()


# -- ordering and concurrency -------------------------------------------


def test_claim_preserves_input_order_and_blocks_later_turns(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        first_message = _message(database, text="one")
        second_message = _message(database, text="two")
        processor = TurnProcessor(database)
        processor.enqueue(
            conversation_id="conv-1", input_message_id=first_message.id
        )
        processor.enqueue(
            conversation_id="conv-1", input_message_id=second_message.id
        )

        first_turn = processor.claim_next(conversation_id="conv-1")
        assert first_turn is not None
        assert first_turn.input_message_id == first_message.id
        # The running earlier turn must not be skipped.
        assert processor.claim_next(conversation_id="conv-1") is None

        processor.complete(first_turn.id, first_turn.claim_token, text="ack-1")

        second_turn = processor.claim_next(conversation_id="conv-1")
        assert second_turn is not None
        assert second_turn.input_message_id == second_message.id
    finally:
        database.dispose()


def test_two_workers_same_conversation_do_not_double_claim(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    first_worker = Database.from_path(migrated_db)
    second_worker = Database.from_path(migrated_db)
    try:
        first_message = _message(database, text="one")
        second_message = _message(database, text="two")
        worker_a = TurnProcessor(database)
        worker_a.enqueue(
            conversation_id="conv-1", input_message_id=first_message.id
        )
        worker_a.enqueue(
            conversation_id="conv-1", input_message_id=second_message.id
        )

        independent_a = TurnProcessor(first_worker)
        independent_b = TurnProcessor(second_worker)

        claimed = independent_a.claim_next(conversation_id="conv-1")
        assert claimed is not None
        # The second worker sees the first claim in a separate connection and
        # cannot start a second running turn in the same conversation.
        assert independent_b.claim_next(conversation_id="conv-1") is None

        independent_a.complete(claimed.id, claimed.claim_token, text="ack-1")

        next_claim = independent_b.claim_next(conversation_id="conv-1")
        assert next_claim is not None
        assert next_claim.id != claimed.id
    finally:
        first_worker.dispose()
        second_worker.dispose()
        database.dispose()


def test_racing_workers_that_select_the_same_candidate_start_one_turn(
    migrated_db: pathlib.Path,
) -> None:
    """Exactly one of two workers that raced the same candidate may win.

    Each worker runs on its own engine. A ``before_cursor_execute`` hook holds
    each worker at a barrier immediately before its first conditional claim
    ``UPDATE``, so both have already selected the same pending turn from their
    own read. After the barrier releases, the single
    ``UPDATE ... WHERE status = 'pending'`` lets exactly one worker claim the
    turn; the other sees ``rowcount == 0``, retries and finds the second
    pending turn still blocked by the now-running first turn, so it returns
    ``None``. Two pending turns in the same conversation make a second claim
    impossible while the first is running.
    """
    database = Database.from_path(migrated_db)
    worker_a_db = Database.from_path(migrated_db)
    worker_b_db = Database.from_path(migrated_db)
    barrier = threading.Barrier(2)
    reached_a = threading.Event()
    reached_b = threading.Event()
    threads: list[threading.Thread] = []
    listeners: list[tuple[Database, object]] = []
    try:
        first_message = _message(database, text="one")
        second_message = _message(database, text="two")
        setup = TurnProcessor(database)
        setup.enqueue(
            conversation_id="conv-1", input_message_id=first_message.id
        )
        setup.enqueue(
            conversation_id="conv-1", input_message_id=second_message.id
        )

        def make_claim_gate(reached: threading.Event):
            def gate(
                conn, cursor, statement, parameters, context, executemany
            ) -> None:
                # Pause only the first conditional claim UPDATE, once per
                # engine. Retries and every later statement stay ungated, so
                # the loser can retry instead of deadlocking on the barrier.
                if (
                    statement.startswith("UPDATE turns")
                    and "status" in statement
                    and not reached.is_set()
                ):
                    reached.set()
                    barrier.wait(timeout=10)

            return gate

        gate_a = make_claim_gate(reached_a)
        gate_b = make_claim_gate(reached_b)
        event.listen(worker_a_db.engine, "before_cursor_execute", gate_a)
        event.listen(worker_b_db.engine, "before_cursor_execute", gate_b)
        listeners.extend([(worker_a_db, gate_a), (worker_b_db, gate_b)])

        results: dict[str, object] = {}
        errors: dict[str, BaseException] = {}

        def worker(name: str, db: Database) -> None:
            try:
                results[name] = TurnProcessor(db).claim_next(
                    conversation_id="conv-1"
                )
            except BaseException as error:  # forwarded to the main thread
                errors[name] = error

        threads = [
            threading.Thread(target=worker, args=("a", worker_a_db)),
            threading.Thread(target=worker, args=("b", worker_b_db)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == {}
        assert not any(thread.is_alive() for thread in threads)

        claims = [results.get("a"), results.get("b")]
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1
        assert claims.count(None) == 1
        winner = winners[0]
        assert winner.status == "running"
        assert winner.claim_token is not None

        with database.session() as session:
            turns = list(session.scalars(select(Turn).order_by(Turn.sequence)))
        assert [turn.status for turn in turns] == ["running", "pending"]
        assert turns[0].id == winner.id
        assert turns[0].claim_token == winner.claim_token
    finally:
        for listener_db, gate in listeners:
            event.remove(
                listener_db.engine, "before_cursor_execute", gate
            )
        for thread in threads:
            thread.join(timeout=30)
        worker_a_db.dispose()
        worker_b_db.dispose()
        database.dispose()


def test_another_conversation_proceeds_while_one_is_blocked(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        message_a = _message(database, conversation_id="conv-a")
        message_b = _message(database, conversation_id="conv-b")
        processor = TurnProcessor(database)
        processor.enqueue(
            conversation_id="conv-a", input_message_id=message_a.id
        )
        processor.enqueue(
            conversation_id="conv-b", input_message_id=message_b.id
        )

        running = processor.claim_next(conversation_id="conv-a")
        assert running is not None

        other = processor.claim_next()
        assert other is not None
        assert other.conversation_id == "conv-b"
    finally:
        database.dispose()


# -- handler execution ---------------------------------------------------


def test_successful_one_pass_reply_after_reopen(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    message = _message(database, text="ping")
    enqueued = TurnProcessor(database).enqueue(
        conversation_id="conv-1", input_message_id=message.id
    )
    database.dispose()

    reopened = Database.from_path(migrated_db)
    try:
        processor = TurnProcessor(reopened)
        result = processor.run_once(
            lambda turn_input: f"echo:{turn_input.text}"
        )

        assert result.status == "completed"
        assert result.turn is not None
        assert result.turn.id == enqueued.turn.id
        assert result.turn.status == "completed"
        assert result.turn.attempt_count == 1
        assert result.reply is not None
        assert result.reply.role == "assistant"
        assert result.reply.text == "echo:ping"
        assert result.reply.source == "secretary"

        messages = ConversationRepository(reopened).list_messages("conv-1")
        assert [(m.role, m.text) for m in messages] == [
            ("user", "ping"),
            ("assistant", "echo:ping"),
        ]
    finally:
        reopened.dispose()


def test_handler_receives_input_identity(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        enqueued = _enqueue(database, text="payload")
        processor = TurnProcessor(database)

        captured = {}

        def handler(turn_input):
            captured["input"] = turn_input
            return "done"

        result = processor.run_once(handler)
        assert result.status == "completed"
        turn_input = captured["input"]
        assert turn_input.turn_id == enqueued.turn.id
        assert turn_input.conversation_id == "conv-1"
        assert turn_input.input_message_id == enqueued.turn.input_message_id
        assert turn_input.role == "user"
        assert turn_input.text == "payload"
        assert turn_input.attempt_count == 1
    finally:
        database.dispose()


def test_run_once_is_idle_without_pending_turns(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        result = TurnProcessor(database).run_once(lambda turn_input: "never")
        assert result.status == "idle"
        assert result.turn is None
        assert result.reply is None
    finally:
        database.dispose()


def test_handler_exception_is_retained_and_retryable(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _enqueue(database, text="hello")
        processor = TurnProcessor(database)

        def failing_handler(turn_input):
            raise RuntimeError("model unavailable")

        failed = processor.run_once(failing_handler)
        assert failed.status == "failed"
        assert failed.error == "RuntimeError: model unavailable"
        assert failed.turn is not None
        # The returned turn is the stored failed row, not the stale running
        # claim snapshot.
        assert failed.turn.status == "failed"
        assert failed.turn.error == "RuntimeError: model unavailable"

        turn = processor.get_turn(failed.turn.id)
        assert turn is not None
        assert turn.status == "failed"
        assert turn.attempt_count == 1
        assert turn.reply_message_id is None
        assert turn.error == "RuntimeError: model unavailable"
        # No fabricated reply is appended.
        assert [
            message.role
            for message in ConversationRepository(database).list_messages(
                "conv-1"
            )
        ] == ["user"]

        # The failed earlier turn blocks later work until explicitly retried.
        assert processor.claim_next(conversation_id="conv-1") is None

        retried = processor.retry_failed(turn.id)
        assert retried.status == "pending"
        assert retried.claim_token is None
        assert retried.error is None
        assert retried.attempt_count == 1

        result = processor.run_once(lambda turn_input: "recovered")
        assert result.status == "completed"
        assert result.turn is not None
        assert result.turn.attempt_count == 2

        messages = ConversationRepository(database).list_messages("conv-1")
        assert [message.text for message in messages] == ["hello", "recovered"]
    finally:
        database.dispose()


def test_retry_failed_requires_failed_status(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _enqueue(database)
        processor = TurnProcessor(database)
        turn = processor.claim_next(conversation_id="conv-1")
        assert turn is not None

        with pytest.raises(TurnStateError):
            processor.retry_failed(turn.id)
    finally:
        database.dispose()


# -- explicit recovery and stale-token fencing ---------------------------


def test_recover_abandoned_is_caller_authorized(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _enqueue(database)
        processor = TurnProcessor(database)
        turn = processor.claim_next(conversation_id="conv-1")
        assert turn is not None
        assert turn.claim_token is not None

        with pytest.raises(StaleClaimError):
            processor.recover_abandoned(turn.id, claim_token="not-the-token")
        assert processor.get_turn(turn.id).status == "running"

        recovered = processor.recover_abandoned(
            turn.id, claim_token=turn.claim_token
        )
        assert recovered.status == "pending"
        assert recovered.claim_token is None

        # The old token can no longer complete the reopened turn.
        with pytest.raises(StaleClaimError):
            processor.complete(turn.id, turn.claim_token, text="stale")
        # A second recovery is no longer a running turn.
        with pytest.raises(StaleClaimError):
            processor.recover_abandoned(turn.id, claim_token=turn.claim_token)

        reclaimed = processor.claim_next(conversation_id="conv-1")
        assert reclaimed is not None
        assert reclaimed.id == turn.id
        assert reclaimed.attempt_count == 2
    finally:
        database.dispose()


def test_stale_token_cannot_finish_or_fail_newer_claim(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _enqueue(database)
        processor = TurnProcessor(database)
        first = processor.claim_next(conversation_id="conv-1")
        assert first is not None
        stale_token = first.claim_token
        assert stale_token is not None

        processor.recover_abandoned(first.id, claim_token=stale_token)
        second = processor.claim_next(conversation_id="conv-1")
        assert second is not None
        assert second.claim_token != stale_token

        with pytest.raises(StaleClaimError):
            processor.complete(first.id, stale_token, text="must not land")
        with pytest.raises(StaleClaimError):
            processor.fail(first.id, stale_token, "must not land")

        current = processor.get_turn(first.id)
        assert current is not None
        assert current.status == "running"
        assert current.claim_token == second.claim_token
        assert current.error is None
        assert [
            message.role
            for message in ConversationRepository(database).list_messages(
                "conv-1"
            )
        ] == ["user"]

        processor.complete(
            second.id, second.claim_token, text="fresh"
        )
        assert [
            message.role
            for message in ConversationRepository(database).list_messages(
                "conv-1"
            )
        ] == ["user", "assistant"]
    finally:
        database.dispose()


def test_repeated_completion_appends_one_reply(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _enqueue(database)
        processor = TurnProcessor(database)
        turn = processor.claim_next(conversation_id="conv-1")
        assert turn is not None

        first = processor.complete(turn.id, turn.claim_token, text="answer")
        second = processor.complete(turn.id, turn.claim_token, text="changed")

        assert first.id == second.id
        assert second.text == "answer"

        messages = ConversationRepository(database).list_messages("conv-1")
        assert [message.role for message in messages] == ["user", "assistant"]

        # A different token cannot complete an already completed turn.
        with pytest.raises(StaleClaimError):
            processor.complete(turn.id, "other-token", text="another")
        assert len(
            ConversationRepository(database).list_messages("conv-1")
        ) == 2
    finally:
        database.dispose()


def test_unknown_turn_ids_raise_not_found(migrated_db: pathlib.Path) -> None:
    database = Database.from_path(migrated_db)
    try:
        processor = TurnProcessor(database)
        with pytest.raises(TurnNotFoundError):
            processor.complete("absent", "token", text="x")
        with pytest.raises(TurnNotFoundError):
            processor.fail("absent", "token", "boom")
        with pytest.raises(TurnNotFoundError):
            processor.retry_failed("absent")
        with pytest.raises(TurnNotFoundError):
            processor.recover_abandoned("absent", claim_token="token")
    finally:
        database.dispose()


def test_concurrent_retry_cannot_overwrite_newer_claim(
    migrated_db: pathlib.Path,
) -> None:
    """A retry that raced a newer retry+claim must not clobber it.

    Worker B starts ``retry_failed`` while the turn is failed and is paused at
    the SQL boundary immediately before its compare-and-set ``UPDATE`` (the
    earlier statements are not paused). Worker A then retries and claims the
    turn with a new token. When B resumes, its conditional update must observe
    that the turn is no longer failed and raise instead of writing ``pending``
    over the newer running claim.
    """
    database = Database.from_path(migrated_db)
    worker_a_db = Database.from_path(migrated_db)
    worker_b_db = Database.from_path(migrated_db)
    listening = False
    try:
        _enqueue(database)
        setup = TurnProcessor(database)
        claimed = setup.claim_next(conversation_id="conv-1")
        assert claimed is not None
        assert claimed.claim_token is not None
        setup.fail(claimed.id, claimed.claim_token, "first attempt")
        turn_id = claimed.id

        reached_update = threading.Event()
        resume_update = threading.Event()
        worker_b_result: dict[str, object] = {}

        def pause_before_retry_update(
            conn, cursor, statement, parameters, context, executemany
        ) -> None:
            # Only the compare-and-set UPDATE is paused; B has already started
            # retry_failed while the row was still failed.
            if statement.startswith("UPDATE turns") and "status" in statement:
                reached_update.set()
                assert resume_update.wait(timeout=30)

        event.listen(
            worker_b_db.engine, "before_cursor_execute", pause_before_retry_update
        )
        listening = True

        def worker_b_retry() -> None:
            try:
                TurnProcessor(worker_b_db).retry_failed(turn_id)
            except Exception as error:  # forwarded to the main thread
                worker_b_result["error"] = error

        thread = threading.Thread(target=worker_b_retry)
        thread.start()
        assert reached_update.wait(timeout=30)

        # A retries and immediately claims the reopened turn with a new token.
        processor_a = TurnProcessor(worker_a_db)
        reopened = processor_a.retry_failed(turn_id)
        assert reopened.status == "pending"
        new_claim = processor_a.claim_next(conversation_id="conv-1")
        assert new_claim is not None
        assert new_claim.claim_token is not None

        resume_update.set()
        thread.join(timeout=30)
        assert not thread.is_alive()

        # B lost the race: it raised and did not overwrite the new claim.
        assert isinstance(worker_b_result.get("error"), TurnStateError)
        stored = TurnProcessor(database).get_turn(turn_id)
        assert stored is not None
        assert stored.status == "running"
        assert stored.claim_token == new_claim.claim_token
    finally:
        if listening:
            event.remove(
                worker_b_db.engine,
                "before_cursor_execute",
                pause_before_retry_update,
            )
        database.dispose()
        worker_a_db.dispose()
        worker_b_db.dispose()


# -- migration preservation ---------------------------------------------


def test_migration_preserves_existing_rows_and_adds_turns(
    db_path: pathlib.Path,
) -> None:
    """The 0003 revision only adds tables; prior input/history/tasks persist."""
    config = alembic_config(db_path)
    command.upgrade(config, "0002_create_conversations_messages")

    database = Database.from_path(db_path)
    try:
        TaskRepository(database).create(
            id="keep", title="keep me", workspace="proj-a"
        )
        repository = ConversationRepository(database)
        repository.create_conversation(id="conv-keep")
        message = repository.append_message(
            conversation_id="conv-keep",
            role="user",
            text="hello",
            source="test",
            external_id="keep-1",
        ).message
    finally:
        database.dispose()

    command.upgrade(config, "head")

    database = Database.from_path(db_path)
    try:
        assert TaskRepository(database).get("keep") is not None

        messages = ConversationRepository(database).list_messages("conv-keep")
        assert [stored.text for stored in messages] == ["hello"]
        assert "turns" in inspect(database.engine).get_table_names()

        # The preserved input can be enqueued and processed after the upgrade.
        result = TurnProcessor(database).enqueue(
            conversation_id="conv-keep", input_message_id=message.id
        )
        assert result.created is True
        assert result.turn.conversation_id == "conv-keep"
    finally:
        database.dispose()
