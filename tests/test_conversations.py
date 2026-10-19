"""Conversation and message storage acceptance tests.

Every test runs against a real migrated temporary file database and the real
migration chain, never an in-memory database.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from agent_manager.storage import (
    Conversation,
    ConversationNotFoundError,
    ConversationRepository,
    Database,
    DuplicateConversationIdError,
    Message,
    MessageConflictError,
    TaskRepository,
)


def _database(db_path: pathlib.Path) -> Database:
    return Database.from_path(db_path)


def test_create_get_and_list_conversations(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        first = repository.create_conversation(
            title="First", metadata={"origin": "api"}
        )
        second = repository.create_conversation()

        fetched = repository.get_conversation(first.id)
        assert fetched is not None
        assert fetched.title == "First"
        assert fetched.meta == {"origin": "api"}
        assert fetched.created_at is not None
        assert fetched.updated_at is not None

        listed = repository.list_conversations()
        assert {conversation.id for conversation in listed} == {
            first.id,
            second.id,
        }
        expected_order = [
            conversation.id
            for conversation in sorted(
                (first, second),
                key=lambda item: (item.created_at, item.id),
            )
        ]
        assert [conversation.id for conversation in listed] == expected_order
        assert repository.get_conversation("missing") is None
    finally:
        database.dispose()


def test_conversation_metadata_round_trips(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        payload = {
            "tags": ["a", "b"],
            "nested": {"deep": [1, 2, {"x": True}]},
        }
        created = repository.create_conversation(metadata=payload)
        fetched = repository.get_conversation(created.id)
        assert fetched is not None
        assert fetched.meta == payload
    finally:
        database.dispose()


def test_duplicate_conversation_id_raises_focused_error(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        repository.create_conversation(id="c-1", title="one")
        with pytest.raises(DuplicateConversationIdError) as excinfo:
            repository.create_conversation(id="c-1", title="two")
        assert excinfo.value.conversation_id == "c-1"
        original = repository.get_conversation("c-1")
        assert original is not None
        assert original.title == "one"
    finally:
        database.dispose()


def test_append_and_list_messages_in_sequence_order(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        conversation = repository.create_conversation(title="chat")
        for index in range(4):
            result = repository.append_message(
                conversation_id=conversation.id,
                role="user",
                text=f"message {index}",
                source="api",
                external_id=f"ext-{index}",
            )
            assert result.inserted is True
            assert result.replayed is False

        messages = repository.list_messages(conversation.id)
        assert [message.text for message in messages] == [
            f"message {index}" for index in range(4)
        ]
        sequences = [message.sequence for message in messages]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
    finally:
        database.dispose()


def test_messages_are_isolated_per_conversation(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        first = repository.create_conversation()
        second = repository.create_conversation()
        repository.append_message(
            conversation_id=first.id, role="user", text="in first"
        )
        repository.append_message(
            conversation_id=second.id, role="user", text="in second"
        )
        assert [m.text for m in repository.list_messages(first.id)] == [
            "in first"
        ]
        assert [m.text for m in repository.list_messages(second.id)] == [
            "in second"
        ]
        # A conversation with no messages, and an absent one, both list empty.
        assert repository.list_messages("absent") == []
    finally:
        database.dispose()


def test_message_metadata_round_trips(migrated_db: pathlib.Path) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        conversation = repository.create_conversation()
        payload = {"provider": "telegram", "update_id": 42, "nested": {"n": 1}}
        repository.append_message(
            conversation_id=conversation.id,
            role="user",
            text="hi",
            metadata=payload,
        )
        message = repository.list_messages(conversation.id)[0]
        assert message.meta == payload
    finally:
        database.dispose()


def test_no_length_caps_on_role_text_or_source(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        conversation = repository.create_conversation(title="x" * 20_000)
        long_role = "r" * 20_000
        long_text = "t" * 20_000
        long_source = "s" * 20_000
        repository.append_message(
            conversation_id=conversation.id,
            role=long_role,
            text=long_text,
            source=long_source,
            external_id="e",
        )
        message = repository.list_messages(conversation.id)[0]
        assert message.role == long_role
        assert message.text == long_text
        assert message.source == long_source
        assert repository.get_conversation(conversation.id).title == "x" * 20_000
    finally:
        database.dispose()


def test_exact_retry_returns_original_message(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        conversation = repository.create_conversation()
        payload = {
            "conversation_id": conversation.id,
            "role": "user",
            "text": "hello",
            "source": "telegram",
            "external_id": "update-1",
            "metadata": {"update_id": 1},
        }
        first = repository.append_message(**payload)
        replay = repository.append_message(**payload)

        assert first.replayed is False
        assert replay.replayed is True
        assert replay.inserted is False
        # Generated id and DB sequence are stable across the replay.
        assert replay.message.id == first.message.id
        assert replay.message.sequence == first.message.sequence
        assert replay.message.created_at == first.message.created_at
        assert len(repository.list_messages(conversation.id)) == 1
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "changed",
    [
        {"role": "assistant"},
        {"text": "different"},
        {"metadata": {"update_id": 2}},
    ],
)
def test_conflicting_payload_raises_and_preserves_original(
    migrated_db: pathlib.Path, changed: dict[str, object]
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        conversation = repository.create_conversation()
        base = {
            "conversation_id": conversation.id,
            "role": "user",
            "text": "hello",
            "source": "telegram",
            "external_id": "update-1",
            "metadata": {"update_id": 1},
        }
        first = repository.append_message(**base)
        with pytest.raises(MessageConflictError) as excinfo:
            repository.append_message(**{**base, **changed})
        assert excinfo.value.existing.id == first.message.id

        messages = repository.list_messages(conversation.id)
        assert len(messages) == 1
        assert messages[0].text == "hello"
        assert messages[0].role == "user"
        assert messages[0].meta == {"update_id": 1}
    finally:
        database.dispose()


def test_absent_external_id_creates_a_new_message_each_time(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        conversation = repository.create_conversation()
        first = repository.append_message(
            conversation_id=conversation.id, role="user", text="hi"
        )
        second = repository.append_message(
            conversation_id=conversation.id, role="user", text="hi"
        )
        assert first.replayed is False
        assert second.replayed is False
        assert first.message.id != second.message.id
        assert len(repository.list_messages(conversation.id)) == 2
    finally:
        database.dispose()


def test_same_external_id_is_scoped_by_source(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        conversation = repository.create_conversation()
        api = repository.append_message(
            conversation_id=conversation.id,
            role="user",
            text="from api",
            source="api",
            external_id="same",
        )
        telegram = repository.append_message(
            conversation_id=conversation.id,
            role="user",
            text="from telegram",
            source="telegram",
            external_id="same",
        )
        assert api.message.id != telegram.message.id
        assert len(repository.list_messages(conversation.id)) == 2
    finally:
        database.dispose()


def test_same_external_id_is_scoped_by_conversation(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        first = repository.create_conversation()
        second = repository.create_conversation()
        left = repository.append_message(
            conversation_id=first.id,
            role="user",
            text="left",
            external_id="same",
        )
        right = repository.append_message(
            conversation_id=second.id,
            role="user",
            text="right",
            external_id="same",
        )
        assert left.message.id != right.message.id
        assert len(repository.list_messages(first.id)) == 1
        assert len(repository.list_messages(second.id)) == 1
    finally:
        database.dispose()


def test_append_to_absent_conversation_raises_not_found(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        with pytest.raises(ConversationNotFoundError) as excinfo:
            repository.append_message(
                conversation_id="missing",
                role="user",
                text="hi",
                external_id="e",
            )
        assert excinfo.value.conversation_id == "missing"
        assert repository.list_messages("missing") == []
    finally:
        database.dispose()


def test_messages_do_not_create_tasks(migrated_db: pathlib.Path) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        tasks = TaskRepository(database)
        conversation = conversations.create_conversation()
        conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="please do the thing",
            external_id="e",
        )
        assert tasks.list() == []
    finally:
        database.dispose()


def test_message_order_is_stable_for_identical_timestamps(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        conversation = repository.create_conversation(title="ordering")
        for index in range(5):
            repository.append_message(
                conversation_id=conversation.id,
                role="user",
                text=f"m{index}",
                external_id=f"e{index}",
            )
        identical = dt.datetime(2030, 1, 1, 0, 0, 0)
        with database.session() as session:
            session.execute(update(Message).values(created_at=identical))

        messages = repository.list_messages(conversation.id)
        assert [message.text for message in messages] == [
            f"m{index}" for index in range(5)
        ]
        timestamps = {message.created_at for message in messages}
        assert timestamps == {identical}
    finally:
        database.dispose()


def test_foreign_key_is_enforced(migrated_db: pathlib.Path) -> None:
    database = _database(migrated_db)
    try:
        with pytest.raises(IntegrityError):
            with database.session() as session:
                session.execute(
                    Message.__table__.insert().values(
                        id="orphan",
                        conversation_id="missing",
                        role="user",
                        text="hi",
                        source="api",
                    )
                )
    finally:
        database.dispose()


def test_close_and_reopen_retains_conversations_and_messages(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    repository = ConversationRepository(database)
    conversation = repository.create_conversation(
        title="durable", metadata={"k": "v"}
    )
    repository.append_message(
        conversation_id=conversation.id,
        role="user",
        text="hello",
        external_id="e",
        metadata={"n": 1},
    )
    database.dispose()

    reopened = _database(migrated_db)
    try:
        repository = ConversationRepository(reopened)
        fetched = repository.get_conversation(conversation.id)
        assert fetched is not None
        assert fetched.title == "durable"
        assert fetched.meta == {"k": "v"}
        messages = repository.list_messages(conversation.id)
        assert len(messages) == 1
        assert messages[0].text == "hello"
        assert messages[0].meta == {"n": 1}
    finally:
        reopened.dispose()


def test_append_advances_conversation_updated_at(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    try:
        repository = ConversationRepository(database)
        conversation = repository.create_conversation()
        past = dt.datetime(2000, 1, 1)
        with database.session() as session:
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation.id)
                .values(updated_at=past)
            )
        repository.append_message(
            conversation_id=conversation.id,
            role="user",
            text="hi",
            external_id="stable",
        )
        advanced = repository.get_conversation(conversation.id).updated_at
        assert advanced > past

        # A replayed append is a no-op and must not move updated_at again.
        replay = repository.append_message(
            conversation_id=conversation.id,
            role="user",
            text="hi",
            external_id="stable",
        )
        assert replay.replayed is True
        assert repository.get_conversation(conversation.id).updated_at == advanced
    finally:
        database.dispose()


def test_concurrent_independent_writers_do_not_lose_messages(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    repository = ConversationRepository(database)
    conversation = repository.create_conversation(title="concurrent")
    workers = 6
    per_worker = 10
    try:

        def append_batch(worker: int) -> None:
            for index in range(per_worker):
                repository.append_message(
                    conversation_id=conversation.id,
                    role="user",
                    text=f"worker-{worker}-{index}",
                    source="api",
                    external_id=f"w{worker}-{index}",
                )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(append_batch, range(workers)))

        messages = repository.list_messages(conversation.id)
        assert len(messages) == workers * per_worker
        assert len({message.id for message in messages}) == workers * per_worker
        sequences = [message.sequence for message in messages]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
    finally:
        database.dispose()


def test_concurrent_identical_delivery_stores_one_message(
    migrated_db: pathlib.Path,
) -> None:
    database = _database(migrated_db)
    repository = ConversationRepository(database)
    conversation = repository.create_conversation(title="dedupe")
    payload = {
        "conversation_id": conversation.id,
        "role": "user",
        "text": "hello",
        "source": "telegram",
        "external_id": "update-1",
        "metadata": {"update_id": 1},
    }

    def deliver(_: int):
        return repository.append_message(**payload)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(deliver, range(8)))

        messages = repository.list_messages(conversation.id)
        assert len(messages) == 1
        assert {result.message.id for result in results} == {messages[0].id}
        assert sum(1 for result in results if result.inserted) == 1
        assert sum(1 for result in results if result.replayed) == 7
    finally:
        database.dispose()
