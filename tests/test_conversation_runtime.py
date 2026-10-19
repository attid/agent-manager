"""Acceptance tests for the per-conversation secretary session binding.

The tests use the real migrated file database, the real profile/conversation
repositories, the real ``TurnProcessor`` and real SQLite connections. No
model, HTTP endpoint or worker is involved.
"""

from __future__ import annotations

import pathlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from agent_manager.secretary import TurnProcessor
from agent_manager.storage import (
    AgentProfileRepository,
    ConversationRepository,
    ConversationRuntimeAgentConflictError,
    ConversationRuntimeAttachmentError,
    ConversationRuntimeBlockedError,
    ConversationRuntimeBusyError,
    ConversationRuntimeNotFoundError,
    ConversationRuntimeOwnershipError,
    ConversationRuntimeReferenceError,
    ConversationRuntimeRepository,
    ConversationRuntimeSessionConflictError,
    ConversationRuntimeTurnStateError,
    Database,
)
from agent_manager.storage.models import ConversationRuntime

from .conftest import alembic_config

PROVENANCE = {"model": "fake-model", "run_id": "run-1"}


def _profile(
    database: Database,
    *,
    agent_id: str = "triage",
    system_prompt: str = "You are the triage agent.",
) -> None:
    AgentProfileRepository(database).create(
        agent_id=agent_id,
        name="Triage",
        role="worker",
        system_prompt=system_prompt,
    )


def _running_turn(
    database: Database,
    *,
    conversation_id: str = "conv-1",
    text: str = "hello",
) -> tuple[TurnProcessor, object]:
    conversations = ConversationRepository(database)
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


def _fresh_conversation(
    database: Database,
    *,
    conversation_id: str = "conv-1",
    agent_id: str = "triage",
) -> None:
    ConversationRepository(database).create_conversation(id=conversation_id)
    _profile(database, agent_id=agent_id)


# -- migration and schema -------------------------------------------------


def test_fresh_migration_creates_conversation_runtimes_schema(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        inspector = inspect(database.engine)
        assert "conversation_runtimes" in inspector.get_table_names()
        columns = {
            column["name"]
            for column in inspector.get_columns("conversation_runtimes")
        }
        assert columns == {
            "conversation_id",
            "agent_id",
            "profile_revision",
            "role_export_path",
            "snapshot_export_path",
            "prompt_export_path",
            "runner_state_dir",
            "runner_session_id",
            "reservation_token",
            "reservation_turn_id",
            "reservation_attempt_count",
            "blocked_reason",
            "last_turn_id",
            "last_reply_text",
            "last_provenance",
            "created_at",
            "updated_at",
        }
        primary_key = inspector.get_pk_constraint("conversation_runtimes")
        assert primary_key["constrained_columns"] == ["conversation_id"]

        foreign_keys = inspector.get_foreign_keys("conversation_runtimes")
        referred_tables = {fk["referred_table"] for fk in foreign_keys}
        assert "conversations" in referred_tables
        assert "agent_profiles" in referred_tables
        profile_fk = next(
            fk
            for fk in foreign_keys
            if fk["referred_table"] == "agent_profiles"
        )
        assert profile_fk["constrained_columns"] == [
            "agent_id",
            "profile_revision",
        ]
        assert profile_fk["referred_columns"] == ["agent_id", "revision"]
    finally:
        database.dispose()


def test_upgrade_from_0004_preserves_rows_and_adds_runtime(
    db_path: pathlib.Path,
) -> None:
    """The 0005 revision is additive; existing durable rows must survive."""
    config = alembic_config(db_path)
    command.upgrade(config, "0004_create_agent_profiles")

    database = Database.from_path(db_path)
    try:
        _profile(database, agent_id="keep-agent")
        conversations = ConversationRepository(database)
        conversations.create_conversation(id="keep-conv")
        message = conversations.append_message(
            conversation_id="keep-conv",
            role="user",
            text="keep me",
            source="test",
        ).message
        turn = TurnProcessor(database).enqueue(
            conversation_id="keep-conv", input_message_id=message.id
        ).turn
        assert "conversation_runtimes" not in inspect(
            database.engine
        ).get_table_names()
    finally:
        database.dispose()

    command.upgrade(config, "head")

    connection = sqlite3.connect(db_path)
    try:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
    finally:
        connection.close()
    # The single head moved to 0009 after analysis reports and
    # recommendations were added; the assertion still verifies exactly one
    # head row records the current schema.
    assert version == [("0009_analysis",)]

    database = Database.from_path(db_path)
    try:
        assert "conversation_runtimes" in inspect(
            database.engine
        ).get_table_names()
        profiles = AgentProfileRepository(database)
        assert profiles.get_revision("keep-agent", 1) is not None
        conversations = ConversationRepository(database)
        assert conversations.get_conversation("keep-conv") is not None
        assert [m.text for m in conversations.list_messages("keep-conv")] == [
            "keep me"
        ]
        assert TurnProcessor(database).get_turn(turn.id) is not None

        # The new boundary is usable immediately after the upgrade.
        processor = TurnProcessor(database)
        claimed = processor.claim_next(conversation_id="keep-conv")
        assert claimed is not None
        reservation = ConversationRuntimeRepository(database).reserve_call(
            conversation_id="keep-conv",
            turn_id=claimed.id,
            attempt_count=claimed.attempt_count,
            agent_id="keep-agent",
            profile_revision=1,
        )
        assert reservation.reserved is True
    finally:
        database.dispose()


# -- read / reopen --------------------------------------------------------


def test_reserve_attach_finish_and_reopen(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        reservation = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        assert reservation.reserved is True
        assert reservation.cached is False
        assert reservation.ownership_token
        token = reservation.ownership_token
        assert reservation.binding.agent_id == "triage"
        assert reservation.binding.profile_revision == 1
        assert reservation.binding.runner_session_id is None

        paths = {
            "role_export_path": "/exports/role.json",
            "snapshot_export_path": "/exports/snapshot.json",
            "prompt_export_path": "/exports/prompt.md",
            "runner_state_dir": "/state/runs",
        }
        attached = repository.attach_prepared(
            conversation_id="conv-1",
            ownership_token=token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            **paths,
        )
        assert attached.role_export_path == "/exports/role.json"
        assert attached.runner_state_dir == "/state/runs"

        finished = repository.finish_call(
            conversation_id="conv-1",
            ownership_token=token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            reply_text="the answer",
            runner_session_id="native-session-1",
            provenance=PROVENANCE,
        )
        assert finished.runner_session_id == "native-session-1"
        assert finished.last_turn_id == turn.id
        assert finished.last_reply_text == "the answer"
        assert finished.last_provenance == PROVENANCE
        assert finished.reservation_token is None
        assert finished.reservation_turn_id is None
        assert finished.reservation_attempt_count is None
    finally:
        database.dispose()

    reopened = Database.from_path(migrated_db)
    try:
        binding = ConversationRuntimeRepository(reopened).get_binding(
            "conv-1"
        )
        assert binding is not None
        assert binding.agent_id == "triage"
        assert binding.profile_revision == 1
        assert binding.role_export_path == "/exports/role.json"
        assert binding.snapshot_export_path == "/exports/snapshot.json"
        assert binding.prompt_export_path == "/exports/prompt.md"
        assert binding.runner_state_dir == "/state/runs"
        assert binding.runner_session_id == "native-session-1"
        assert binding.last_turn_id == turn.id
        assert binding.last_reply_text == "the answer"
        assert binding.last_provenance == PROVENANCE
        assert binding.blocked_reason is None
        assert binding.created_at is not None
        assert binding.updated_at is not None
    finally:
        reopened.dispose()


# -- concurrency ----------------------------------------------------------


def test_two_connections_simultaneous_reservation_yields_one_owner(
    migrated_db: pathlib.Path,
) -> None:
    setup = Database.from_path(migrated_db)
    try:
        _fresh_conversation(setup)
        _, turn = _running_turn(setup)
    finally:
        setup.dispose()

    barrier = threading.Barrier(2)

    def attempt() -> object:
        database = Database.from_path(migrated_db)
        try:
            repository = ConversationRuntimeRepository(database)
            barrier.wait()
            try:
                return repository.reserve_call(
                    conversation_id="conv-1",
                    turn_id=turn.id,
                    attempt_count=turn.attempt_count,
                    agent_id="triage",
                    profile_revision=1,
                )
            except ConversationRuntimeBusyError as error:
                return error
        finally:
            database.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            executor.submit(attempt),
            executor.submit(attempt),
        ]
        results = [future.result() for future in outcomes]

    reserved = [
        result
        for result in results
        if not isinstance(result, ConversationRuntimeBusyError)
    ]
    busy = [
        result
        for result in results
        if isinstance(result, ConversationRuntimeBusyError)
    ]
    assert len(reserved) == 1
    assert len(busy) == 1
    token = reserved[0].ownership_token
    assert token

    check = Database.from_path(migrated_db)
    try:
        binding = ConversationRuntimeRepository(check).get_binding("conv-1")
        assert binding is not None
        assert binding.reservation_token == token
        assert binding.reservation_turn_id == turn.id
        assert binding.reservation_attempt_count == turn.attempt_count
    finally:
        check.dispose()


# -- pinned profile -------------------------------------------------------


def test_pinned_revision_survives_registry_update(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        first = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        repository.finish_call(
            conversation_id="conv-1",
            ownership_token=first.ownership_token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            reply_text="first",
            runner_session_id="session-1",
        )
        processor.complete(turn.id, turn.claim_token, text="first")

        profiles = AgentProfileRepository(database)
        profiles.update(agent_id="triage", expected_revision=1, name="Triage 2")

        _, second_turn = _running_turn(database, text="second")
        second = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=second_turn.id,
            attempt_count=second_turn.attempt_count,
            agent_id="triage",
            profile_revision=2,
        )
        # The requested new revision is ignored: revision 1 stays pinned.
        assert second.reserved is True
        assert second.binding.profile_revision == 1

        repository.finish_call(
            conversation_id="conv-1",
            ownership_token=second.ownership_token,
            turn_id=second_turn.id,
            attempt_count=second_turn.attempt_count,
            reply_text="second",
            runner_session_id="session-1",
        )
        processor.complete(second_turn.id, second_turn.claim_token, text="second")

        _, third_turn = _running_turn(database, text="third")
        with pytest.raises(ConversationRuntimeAgentConflictError):
            repository.reserve_call(
                conversation_id="conv-1",
                turn_id=third_turn.id,
                attempt_count=third_turn.attempt_count,
                agent_id="other-agent",
                profile_revision=1,
            )
        assert repository.get_binding("conv-1").agent_id == "triage"
        assert repository.get_binding("conv-1").profile_revision == 1
    finally:
        database.dispose()


# -- cached completed result ---------------------------------------------


def test_completed_result_cache_after_turn_recovery_avoids_reservation(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        first = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        repository.finish_call(
            conversation_id="conv-1",
            ownership_token=first.ownership_token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            reply_text="cached answer",
            runner_session_id="session-1",
            provenance=PROVENANCE,
        )

        # Crash recovery: the result is stored but TurnProcessor.complete has
        # not run, so the turn is recovered and claimed again.
        processor.recover_abandoned(turn.id, claim_token=turn.claim_token)
        recovered = processor.claim_next(conversation_id="conv-1")
        assert recovered is not None
        assert recovered.attempt_count == turn.attempt_count + 1

        cached = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=recovered.id,
            attempt_count=recovered.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        assert cached.reserved is False
        assert cached.cached is True
        assert cached.ownership_token is None
        assert cached.reply_text == "cached answer"
        assert cached.provenance == PROVENANCE

        binding = repository.get_binding("conv-1")
        assert binding is not None
        assert binding.reservation_token is None
        assert binding.runner_session_id == "session-1"

        # Completing the recovered turn uses the cached reply, so the native
        # session id was not advanced a second time.
        reply = processor.complete(
            recovered.id, recovered.claim_token or "", text=cached.reply_text
        )
        assert reply.text == "cached answer"
        assert repository.get_binding("conv-1").runner_session_id == "session-1"
    finally:
        database.dispose()


# -- stale attempt / ownership -------------------------------------------


def test_stale_attempt_cannot_finish_after_turn_recovery(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        reservation = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        processor.recover_abandoned(turn.id, claim_token=turn.claim_token)
        recovered = processor.claim_next(conversation_id="conv-1")
        assert recovered is not None

        with pytest.raises(ConversationRuntimeTurnStateError):
            repository.finish_call(
                conversation_id="conv-1",
                ownership_token=reservation.ownership_token,
                turn_id=turn.id,
                attempt_count=turn.attempt_count,
                reply_text="stale",
                runner_session_id="session-1",
                provenance=PROVENANCE,
            )

        binding = repository.get_binding("conv-1")
        assert binding is not None
        assert binding.last_turn_id is None
        assert binding.runner_session_id is None
        assert binding.reservation_token == reservation.ownership_token

        # The abandoned reservation still blocks the recovered attempt.
        with pytest.raises(ConversationRuntimeBusyError):
            repository.reserve_call(
                conversation_id="conv-1",
                turn_id=recovered.id,
                attempt_count=recovered.attempt_count,
                agent_id="triage",
                profile_revision=1,
            )
    finally:
        database.dispose()


def test_stale_token_cannot_attach_finish_or_replace_new_reservation(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        first = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        repository.attach_prepared(
            conversation_id="conv-1",
            ownership_token=first.ownership_token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            role_export_path="/first/role.json",
            snapshot_export_path="/first/snapshot.json",
            prompt_export_path="/first/prompt.md",
            runner_state_dir="/first/state",
        )
        repository.finish_call(
            conversation_id="conv-1",
            ownership_token=first.ownership_token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            reply_text="first",
            runner_session_id="session-1",
        )
        processor.complete(turn.id, turn.claim_token, text="first")

        _, second_turn = _running_turn(database, text="second")
        second = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=second_turn.id,
            attempt_count=second_turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )

        with pytest.raises(ConversationRuntimeOwnershipError):
            repository.attach_prepared(
                conversation_id="conv-1",
                ownership_token=first.ownership_token,
                role_export_path="/stale/role.json",
                snapshot_export_path="/stale/snapshot.json",
                prompt_export_path="/stale/prompt.md",
                runner_state_dir="/stale/state",
            )
        with pytest.raises(ConversationRuntimeOwnershipError):
            repository.finish_call(
                conversation_id="conv-1",
                ownership_token=first.ownership_token,
                turn_id=turn.id,
                attempt_count=turn.attempt_count,
                reply_text="stale",
                runner_session_id="session-2",
            )

        binding = repository.get_binding("conv-1")
        assert binding is not None
        assert binding.reservation_token == second.ownership_token
        assert binding.last_turn_id == turn.id
        assert binding.runner_session_id == "session-1"
        assert binding.role_export_path == "/first/role.json"
    finally:
        database.dispose()


# -- attachment -----------------------------------------------------------


def test_attach_paths_once_and_preserve_across_reservations(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        first = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        paths = dict(
            role_export_path="/first/role.json",
            snapshot_export_path="/first/snapshot.json",
            prompt_export_path="/first/prompt.md",
            runner_state_dir="/first/state",
        )
        attached = repository.attach_prepared(
            conversation_id="conv-1",
            ownership_token=first.ownership_token,
            **paths,
        )
        assert attached.role_export_path == "/first/role.json"

        # An identical retry with the current token is idempotent.
        retried = repository.attach_prepared(
            conversation_id="conv-1",
            ownership_token=first.ownership_token,
            **paths,
        )
        assert retried.role_export_path == "/first/role.json"

        with pytest.raises(ConversationRuntimeAttachmentError):
            repository.attach_prepared(
                conversation_id="conv-1",
                ownership_token=first.ownership_token,
                role_export_path="/other/role.json",
                snapshot_export_path="/other/snapshot.json",
                prompt_export_path="/other/prompt.md",
                runner_state_dir="/other/state",
            )

        repository.finish_call(
            conversation_id="conv-1",
            ownership_token=first.ownership_token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            reply_text="first",
            runner_session_id="session-1",
        )
        processor.complete(turn.id, turn.claim_token, text="first")

        _, second_turn = _running_turn(database, text="second")
        second = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=second_turn.id,
            attempt_count=second_turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        # A later reservation cannot replace the paths attached once.
        with pytest.raises(ConversationRuntimeAttachmentError):
            repository.attach_prepared(
                conversation_id="conv-1",
                ownership_token=second.ownership_token,
                role_export_path="/second/role.json",
                snapshot_export_path="/second/snapshot.json",
                prompt_export_path="/second/prompt.md",
                runner_state_dir="/second/state",
            )
        assert repository.get_binding("conv-1").role_export_path == (
            "/first/role.json"
        )
    finally:
        database.dispose()


# -- session identity ------------------------------------------------------


def test_resumed_session_cannot_switch_native_session_id(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        first = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        repository.finish_call(
            conversation_id="conv-1",
            ownership_token=first.ownership_token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            reply_text="first",
            runner_session_id="session-1",
        )
        processor.complete(turn.id, turn.claim_token, text="first")

        _, second_turn = _running_turn(database, text="second")
        second = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=second_turn.id,
            attempt_count=second_turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )

        with pytest.raises(ConversationRuntimeSessionConflictError):
            repository.finish_call(
                conversation_id="conv-1",
                ownership_token=second.ownership_token,
                turn_id=second_turn.id,
                attempt_count=second_turn.attempt_count,
                reply_text="second",
                runner_session_id="session-2",
            )

        binding = repository.get_binding("conv-1")
        assert binding is not None
        assert binding.runner_session_id == "session-1"
        assert binding.last_turn_id == turn.id
        assert binding.reservation_token == second.ownership_token

        # The same native session can continue.
        finished = repository.finish_call(
            conversation_id="conv-1",
            ownership_token=second.ownership_token,
            turn_id=second_turn.id,
            attempt_count=second_turn.attempt_count,
            reply_text="second",
            runner_session_id="session-1",
        )
        assert finished.last_turn_id == second_turn.id
    finally:
        database.dispose()


def test_finish_rejects_empty_session_id(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        _, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)
        reservation = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        with pytest.raises(ConversationRuntimeSessionConflictError):
            repository.finish_call(
                conversation_id="conv-1",
                ownership_token=reservation.ownership_token,
                turn_id=turn.id,
                attempt_count=turn.attempt_count,
                reply_text="reply",
                runner_session_id="",
            )
        assert repository.get_binding("conv-1").runner_session_id is None
    finally:
        database.dispose()


# -- block / abandoned -----------------------------------------------------


def test_block_owned_reservation_and_fail_closed(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        reservation = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )

        # A stale token never overwrites the active owner.
        with pytest.raises(ConversationRuntimeOwnershipError):
            repository.block_call(
                conversation_id="conv-1",
                ownership_token="not-the-owner",
                reason="stale",
            )
        assert (
            repository.get_binding("conv-1").reservation_token
            == reservation.ownership_token
        )

        blocked = repository.block_call(
            conversation_id="conv-1",
            ownership_token=reservation.ownership_token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            reason="model call uncertain",
        )
        assert blocked.blocked_reason == "model call uncertain"
        assert blocked.reservation_token is None

        processor.recover_abandoned(turn.id, claim_token=turn.claim_token)
        recovered = processor.claim_next(conversation_id="conv-1")
        assert recovered is not None
        with pytest.raises(ConversationRuntimeBlockedError):
            repository.reserve_call(
                conversation_id="conv-1",
                turn_id=recovered.id,
                attempt_count=recovered.attempt_count,
                agent_id="triage",
                profile_revision=1,
            )
    finally:
        database.dispose()


def test_abandoned_reservation_fails_closed(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        reservation = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        # A crashed worker leaves the token behind; no automatic replay.
        processor.recover_abandoned(turn.id, claim_token=turn.claim_token)
        recovered = processor.claim_next(conversation_id="conv-1")
        assert recovered is not None
        with pytest.raises(ConversationRuntimeBusyError):
            repository.reserve_call(
                conversation_id="conv-1",
                turn_id=recovered.id,
                attempt_count=recovered.attempt_count,
                agent_id="triage",
                profile_revision=1,
            )
        assert (
            repository.get_binding("conv-1").reservation_token
            == reservation.ownership_token
        )
    finally:
        database.dispose()


def test_release_prelaunch_guards_token_attempt_and_running_turn(
    migrated_db: pathlib.Path,
) -> None:
    """The narrow pre-launch release never unlocks anything it does not own."""
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)
        reservation = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )

        # A stale token never releases the active owner.
        with pytest.raises(ConversationRuntimeOwnershipError):
            repository.release_prelaunch_call(
                conversation_id="conv-1",
                ownership_token="not-the-owner",
                turn_id=turn.id,
                attempt_count=turn.attempt_count,
            )
        # A wrong attempt never releases the active owner.
        with pytest.raises(ConversationRuntimeOwnershipError):
            repository.release_prelaunch_call(
                conversation_id="conv-1",
                ownership_token=reservation.ownership_token,
                turn_id=turn.id,
                attempt_count=turn.attempt_count + 1,
            )
        assert (
            repository.get_binding("conv-1").reservation_token
            == reservation.ownership_token
        )

        # The live owner can release without setting the blocked latch.
        released = repository.release_prelaunch_call(
            conversation_id="conv-1",
            ownership_token=reservation.ownership_token,
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
        )
        assert released.reservation_token is None
        assert released.blocked_reason is None
        assert released.last_turn_id is None

        # Once the reserved turn has moved on, the token can no longer
        # release even though the ownership metadata still matches.
        reclaimed = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        assert reclaimed.ownership_token is not None
        processor.recover_abandoned(turn.id, claim_token=turn.claim_token)
        with pytest.raises(ConversationRuntimeTurnStateError):
            repository.release_prelaunch_call(
                conversation_id="conv-1",
                ownership_token=reclaimed.ownership_token,
                turn_id=turn.id,
                attempt_count=turn.attempt_count,
            )
        # The refused release changed nothing and did not block.
        assert (
            repository.get_binding("conv-1").reservation_token
            == reclaimed.ownership_token
        )
        assert repository.get_binding("conv-1").blocked_reason is None
    finally:
        database.dispose()


# -- missing references / FK ----------------------------------------------


def test_missing_references_raise_typed_errors(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        repository = ConversationRuntimeRepository(database)
        with pytest.raises(ConversationRuntimeReferenceError):
            repository.reserve_call(
                conversation_id="ghost-conv",
                turn_id="ghost-turn",
                attempt_count=1,
                agent_id="ghost",
                profile_revision=1,
            )

        ConversationRepository(database).create_conversation(id="conv-1")
        processor, turn = _running_turn(database)
        with pytest.raises(ConversationRuntimeReferenceError):
            repository.reserve_call(
                conversation_id="conv-1",
                turn_id=turn.id,
                attempt_count=turn.attempt_count,
                agent_id="ghost-agent",
                profile_revision=1,
            )

        _profile(database, agent_id="triage")
        with pytest.raises(ConversationRuntimeReferenceError):
            repository.reserve_call(
                conversation_id="conv-1",
                turn_id="ghost-turn",
                attempt_count=1,
                agent_id="triage",
                profile_revision=1,
            )

        with pytest.raises(ConversationRuntimeReferenceError):
            repository.reserve_call(
                conversation_id="conv-1",
                turn_id=turn.id,
                attempt_count=turn.attempt_count,
                agent_id="triage",
                profile_revision=99,
            )

        assert repository.get_binding("ghost-conv") is None
        with pytest.raises(ConversationRuntimeNotFoundError):
            repository.finish_call(
                conversation_id="ghost-conv",
                ownership_token="token",
                turn_id="turn",
                attempt_count=1,
                reply_text="reply",
                runner_session_id="session",
            )
        with pytest.raises(ConversationRuntimeNotFoundError):
            repository.block_call(
                conversation_id="ghost-conv",
                ownership_token="token",
                reason="reason",
            )
    finally:
        database.dispose()


def test_composite_profile_foreign_key_is_enforced(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        ConversationRepository(database).create_conversation(id="conv-1")
        _profile(database, agent_id="triage")
        # Use a raw session so the expected integrity failure can be observed
        # and rolled back without a context-manager commit on a poisoned
        # transaction.
        session = database.session_factory()
        try:
            session.add(
                ConversationRuntime(
                    conversation_id="conv-1",
                    agent_id="triage",
                    profile_revision=99,
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
        finally:
            session.close()
    finally:
        database.dispose()


# -- stale attachment after turn recovery ---------------------------------


def test_attach_after_recovery_is_rejected_without_writing_paths(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        reservation = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        processor.recover_abandoned(turn.id, claim_token=turn.claim_token)
        recovered = processor.claim_next(conversation_id="conv-1")
        assert recovered is not None
        assert recovered.attempt_count == turn.attempt_count + 1

        # The abandoned runtime token still matches its original reservation,
        # but the reserved turn is no longer running at that attempt. Attach
        # must reject even when the optional turn/attempt arguments are
        # omitted.
        with pytest.raises(ConversationRuntimeTurnStateError):
            repository.attach_prepared(
                conversation_id="conv-1",
                ownership_token=reservation.ownership_token,
                role_export_path="/stale/role.json",
                snapshot_export_path="/stale/snapshot.json",
                prompt_export_path="/stale/prompt.md",
                runner_state_dir="/stale/state",
            )

        binding = repository.get_binding("conv-1")
        assert binding is not None
        assert binding.role_export_path is None
        assert binding.snapshot_export_path is None
        assert binding.prompt_export_path is None
        assert binding.runner_state_dir is None
        assert binding.runner_session_id is None
        assert binding.last_turn_id is None
        assert binding.reservation_token == reservation.ownership_token
        assert binding.reservation_turn_id == turn.id
        assert binding.reservation_attempt_count == turn.attempt_count

        # The same stale attachment is rejected when the original turn and
        # attempt are supplied explicitly.
        with pytest.raises(ConversationRuntimeTurnStateError):
            repository.attach_prepared(
                conversation_id="conv-1",
                ownership_token=reservation.ownership_token,
                turn_id=turn.id,
                attempt_count=turn.attempt_count,
                role_export_path="/stale/role.json",
                snapshot_export_path="/stale/snapshot.json",
                prompt_export_path="/stale/prompt.md",
                runner_state_dir="/stale/state",
            )

        # The reservation is still held, so the recovered attempt fails closed.
        with pytest.raises(ConversationRuntimeBusyError):
            repository.reserve_call(
                conversation_id="conv-1",
                turn_id=recovered.id,
                attempt_count=recovered.attempt_count,
                agent_id="triage",
                profile_revision=1,
            )
    finally:
        database.dispose()


def test_attach_identical_paths_after_recovery_is_rejected(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _fresh_conversation(database)
        processor, turn = _running_turn(database)
        repository = ConversationRuntimeRepository(database)

        reservation = repository.reserve_call(
            conversation_id="conv-1",
            turn_id=turn.id,
            attempt_count=turn.attempt_count,
            agent_id="triage",
            profile_revision=1,
        )
        paths = dict(
            role_export_path="/first/role.json",
            snapshot_export_path="/first/snapshot.json",
            prompt_export_path="/first/prompt.md",
            runner_state_dir="/first/state",
        )
        repository.attach_prepared(
            conversation_id="conv-1",
            ownership_token=reservation.ownership_token,
            **paths,
        )

        processor.recover_abandoned(turn.id, claim_token=turn.claim_token)
        recovered = processor.claim_next(conversation_id="conv-1")
        assert recovered is not None
        assert recovered.attempt_count == turn.attempt_count + 1

        # An identical-path retry is only idempotent while the reserved turn
        # is still running at the reserved attempt.
        with pytest.raises(ConversationRuntimeTurnStateError):
            repository.attach_prepared(
                conversation_id="conv-1",
                ownership_token=reservation.ownership_token,
                **paths,
            )

        binding = repository.get_binding("conv-1")
        assert binding is not None
        assert binding.role_export_path == "/first/role.json"
        assert binding.snapshot_export_path == "/first/snapshot.json"
        assert binding.prompt_export_path == "/first/prompt.md"
        assert binding.runner_state_dir == "/first/state"
        assert binding.reservation_token == reservation.ownership_token
    finally:
        database.dispose()
