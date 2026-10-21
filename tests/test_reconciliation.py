"""Acceptance tests for startup reconciliation and crash recovery.

Every test runs against a real migrated temporary file database, the real
``TurnProcessor``, the real ``ConversationRuntimeRepository`` and real SQLite
connections. No model, network or external service is involved.
"""

from __future__ import annotations

import logging
import pathlib

from fastapi.testclient import TestClient

from kuzya.api import create_app
from kuzya.secretary import ReconciliationResult, TurnProcessor
from kuzya.storage import (
    AgentProfileRepository,
    ConversationRepository,
    ConversationRuntimeRepository,
    Database,
)

DEFAULT_REASON = "Turn execution was interrupted by process restart"


def _app(db_path: pathlib.Path):
    """Build an application for a file database path."""
    return create_app(f"sqlite:///{db_path}")


def _conversation(
    database: Database, conversation_id: str = "conv-1"
) -> ConversationRepository:
    repository = ConversationRepository(database)
    if repository.get_conversation(conversation_id) is None:
        repository.create_conversation(id=conversation_id)
    return repository


def _profile(database: Database, *, agent_id: str = "triage") -> None:
    AgentProfileRepository(database).create(
        agent_id=agent_id,
        name="Triage",
        role="worker",
        system_prompt="You are the triage agent.",
    )


def _running_turn(
    database: Database,
    *,
    conversation_id: str = "conv-1",
    text: str = "hello",
):
    conversations = _conversation(database, conversation_id)
    message = conversations.append_message(
        conversation_id=conversation_id,
        role="user",
        text=text,
        source="test",
    ).message
    processor = TurnProcessor(database)
    processor.enqueue(
        conversation_id=conversation_id, input_message_id=message.id
    )
    turn = processor.claim_next(conversation_id=conversation_id)
    assert turn is not None
    return processor, turn


def _reserve(
    database: Database,
    turn,
    *,
    agent_id: str = "triage",
    revision: int = 1,
):
    return ConversationRuntimeRepository(database).reserve_call(
        conversation_id=turn.conversation_id,
        turn_id=turn.id,
        attempt_count=turn.attempt_count,
        agent_id=agent_id,
        profile_revision=revision,
    )


# -- orphan reconciliation ----------------------------------------------


def test_reconcile_orphaned_running_turn_without_runtime(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        processor, turn = _running_turn(database)

        result = processor.reconcile_startup_orphans()

        assert isinstance(result, ReconciliationResult)
        assert result.turn_ids == [turn.id]
        assert result.conversation_ids == ["conv-1"]

        stored = processor.get_turn(turn.id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.error == DEFAULT_REASON
        assert stored.claim_token is None
        assert stored.finished_at is not None
        # No runtime row existed for the conversation, so none is created.
        assert (
            ConversationRuntimeRepository(database).get_binding("conv-1")
            is None
        )
    finally:
        database.dispose()


def test_reconcile_releases_active_reservation_and_blocks(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(database)
        processor, turn = _running_turn(database)
        reservation = _reserve(database, turn)
        assert reservation.reserved is True

        result = processor.reconcile_startup_orphans(
            reason="process restart test"
        )

        assert result.turn_ids == [turn.id]
        assert result.conversation_ids == ["conv-1"]
        stored = processor.get_turn(turn.id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.error == "process restart test"
        assert stored.claim_token is None

        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.reservation_token is None
        assert binding.reservation_turn_id is None
        assert binding.reservation_attempt_count is None
        assert binding.blocked_reason == (
            "Interrupted: process restart test"
        )
    finally:
        database.dispose()


def test_reconcile_multiple_orphans_and_is_idempotent(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(database)
        processor, first = _running_turn(database, conversation_id="conv-1")
        _, second = _running_turn(database, conversation_id="conv-2")
        _reserve(database, first)

        result = processor.reconcile_startup_orphans()
        assert set(result.turn_ids) == {first.id, second.id}
        assert set(result.conversation_ids) == {"conv-1", "conv-2"}

        # A repeated call reconciles nothing.
        again = processor.reconcile_startup_orphans()
        assert again.turn_ids == []
        assert again.conversation_ids == []

        # A clean database is a no-op too.
        assert processor.reconcile_startup_orphans().turn_ids == []
    finally:
        database.dispose()


def test_reconcile_ignores_turns_that_are_not_running(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        processor, turn = _running_turn(database)
        processor.fail(turn.id, turn.claim_token, "handler failed")

        result = processor.reconcile_startup_orphans()

        assert result.turn_ids == []
        stored = processor.get_turn(turn.id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.error == "handler failed"
    finally:
        database.dispose()


def test_reconcile_logs_warning_with_structured_context(
    migrated_db: pathlib.Path,
    caplog,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        processor, turn = _running_turn(database)

        with caplog.at_level(
            logging.WARNING, logger="kuzya.secretary"
        ):
            processor.reconcile_startup_orphans()

        records = [
            record
            for record in caplog.records
            if record.name == "kuzya.secretary"
        ]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.turn_id == turn.id
        assert record.conversation_id == "conv-1"
        assert record.attempt_count == turn.attempt_count
    finally:
        database.dispose()


# -- explicit retry ------------------------------------------------------


def test_retry_failed_unblocks_interrupted_conversation(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(database)
        processor, turn = _running_turn(database)
        _reserve(database, turn)
        processor.reconcile_startup_orphans()

        reopened = processor.retry_failed(turn.id)
        assert reopened.status == "pending"
        assert reopened.claim_token is None
        assert reopened.error is None

        repository = ConversationRuntimeRepository(database)
        binding = repository.get_binding("conv-1")
        assert binding is not None
        assert binding.blocked_reason is None

        # The reopened turn can be claimed and acquire a fresh reservation.
        claimed = processor.claim_next(conversation_id="conv-1")
        assert claimed is not None
        assert claimed.attempt_count == 2
        fresh = _reserve(database, claimed)
        assert fresh.reserved is True
        assert fresh.ownership_token is not None

        repository.finish_call(
            conversation_id="conv-1",
            ownership_token=fresh.ownership_token,
            turn_id=claimed.id,
            attempt_count=claimed.attempt_count,
            reply_text="recovered",
            runner_session_id="native-session-recovered",
        )
        reply = processor.complete(
            claimed.id, claimed.claim_token, text="recovered"
        )
        assert reply.text == "recovered"
        messages = ConversationRepository(database).list_messages("conv-1")
        assert [message.text for message in messages] == [
            "hello",
            "recovered",
        ]
    finally:
        database.dispose()


def test_retry_failed_keeps_uncertain_call_block(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(database)
        processor, turn = _running_turn(database)
        reservation = _reserve(database, turn)
        repository = ConversationRuntimeRepository(database)
        repository.block_call(
            conversation_id="conv-1",
            ownership_token=reservation.ownership_token,
            reason="PiReplyFailedError: uncertain child failure",
        )
        processor.fail(turn.id, turn.claim_token, "child failed")

        processor.retry_failed(turn.id)

        binding = repository.get_binding("conv-1")
        assert binding is not None
        # A non-interruption block stays fail-closed across an explicit retry.
        assert binding.blocked_reason == (
            "PiReplyFailedError: uncertain child failure"
        )
    finally:
        database.dispose()


# -- lifespan integration ------------------------------------------------


def test_startup_lifespan_reconciles_crash_orphans(
    migrated_db: pathlib.Path,
    caplog,
) -> None:
    # Simulate a process that crashed with a running turn and a reservation.
    database = Database.from_path(migrated_db)
    try:
        _profile(database)
        processor, turn = _running_turn(database)
        _reserve(database, turn)
    finally:
        database.dispose()

    with caplog.at_level(logging.INFO, logger="kuzya.api.app"):
        with TestClient(_app(migrated_db)) as client:
            assert client.get("/health").status_code == 200

    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "startup_reconciliation"
    ]
    assert len(events) == 1
    assert events[0].levelno == logging.WARNING
    assert events[0].reconciled_turns == 1

    database = Database.from_path(migrated_db)
    try:
        stored = TurnProcessor(database).get_turn(turn.id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.error == DEFAULT_REASON
        assert stored.claim_token is None
        binding = ConversationRuntimeRepository(database).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.reservation_token is None
        assert binding.reservation_turn_id is None
        assert binding.blocked_reason == f"Interrupted: {DEFAULT_REASON}"
    finally:
        database.dispose()


def test_startup_lifespan_logs_info_without_orphans(
    migrated_db: pathlib.Path,
    caplog,
) -> None:
    with caplog.at_level(logging.INFO, logger="kuzya.api.app"):
        with TestClient(_app(migrated_db)) as client:
            assert client.get("/health").status_code == 200

    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "startup_reconciliation"
    ]
    assert len(events) == 1
    assert events[0].levelno == logging.INFO
    assert events[0].reconciled_turns == 0
