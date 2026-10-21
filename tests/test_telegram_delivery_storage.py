"""Durable Telegram message links and delivery outbox: storage behaviour.

Covers migration shape, the transactional message hooks (append / complete /
fail), idempotent re-registration, per-conversation claim ordering, part
progress and restart recovery. All tests run against real temporary files,
never against an in-memory SQLite database.
"""

from __future__ import annotations

import sqlalchemy

from agent_manager.secretary import TurnProcessor
from agent_manager.storage import Database
from agent_manager.storage.models import TelegramMessageLink
from agent_manager.storage.repository import ConversationRepository
from agent_manager.storage.telegram_delivery import (
    DELIVERY_KIND_API_QUESTION,
    DELIVERY_KIND_REPLY,
    DELIVERY_KIND_TURN_ERROR,
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENDING,
    DELIVERY_STATUS_SENT,
    INPUT_SOURCE_API,
    INPUT_SOURCE_TASK_EVENT,
    INPUT_SOURCE_TELEGRAM,
    MESSAGE_ORIGIN_USER,
    TelegramDeliveryRepository,
    api_question_delivery_key,
    reply_delivery_key,
    turn_error_delivery_key,
)


def _database(migrated_db) -> Database:
    return Database.from_path(migrated_db)


def _claim_and_complete(processor: TurnProcessor, conversation_id: str, message_id: str):
    processor.enqueue(conversation_id=conversation_id, input_message_id=message_id)
    turn = processor.claim_next(conversation_id=conversation_id)
    assert turn is not None
    return turn, processor.complete(turn.id, turn.claim_token, text="Готово")


def test_migration_creates_link_and_delivery_tables(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        inspector = sqlalchemy.inspect(database.engine)
        tables = set(inspector.get_table_names())
        assert {"telegram_message_links", "telegram_deliveries"} <= tables

        link_columns = {c["name"] for c in inspector.get_columns("telegram_message_links")}
        assert link_columns == {
            "chat_id",
            "telegram_message_id",
            "conversation_id",
            "message_id",
            "turn_id",
            "task_id",
            "origin",
            "created_at",
        }
        delivery_columns = {c["name"] for c in inspector.get_columns("telegram_deliveries")}
        assert delivery_columns == {
            "sequence",
            "id",
            "idempotency_key",
            "conversation_id",
            "message_id",
            "turn_id",
            "kind",
            "chat_id",
            "status",
            "parts_total",
            "parts_sent",
            "telegram_message_ids",
            "attempt_count",
            "error",
            "metadata",
            "created_at",
            "updated_at",
            "claimed_at",
            "sent_at",
        }
    finally:
        database.dispose()


def test_telegram_append_records_user_link(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation()
        result = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Привет из Telegram",
            source=INPUT_SOURCE_TELEGRAM,
            metadata={
                "chat_id": 12345,
                "telegram_message_id": 777,
                "task_id": "abc-task",
            },
        )
        repo = TelegramDeliveryRepository(database)
        link = repo.get_message_link(12345, 777)
        assert link is not None
        assert link.conversation_id == conversation.id
        assert link.message_id == result.message.id
        assert link.task_id == "abc-task"
        assert link.origin == MESSAGE_ORIGIN_USER

        # Re-registering the same Telegram message is a no-op.
        repo.record_message_link(
            chat_id=12345,
            telegram_message_id=777,
            conversation_id=conversation.id,
            origin=MESSAGE_ORIGIN_USER,
        )
        assert repo.list_deliveries() == []
        with database.session() as session:
            count = session.scalar(
                sqlalchemy.select(sqlalchemy.func.count()).select_from(
                    TelegramMessageLink
                )
            )
        assert count == 1
    finally:
        database.dispose()


def test_telegram_append_without_chat_metadata_records_nothing(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation()
        conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Нет метаданных чата",
            source=INPUT_SOURCE_TELEGRAM,
        )
        repo = TelegramDeliveryRepository(database)
        assert repo.list_deliveries() == []
    finally:
        database.dispose()


def test_api_user_question_enqueues_delivery(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation()
        result = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Вопрос через API",
            source=INPUT_SOURCE_API,
            verified_caller="execution:agy|build",
        )
        repo = TelegramDeliveryRepository(database)
        rows = repo.list_deliveries(conversation_id=conversation.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == DELIVERY_KIND_API_QUESTION
        assert row.status == DELIVERY_STATUS_PENDING
        assert row.idempotency_key == api_question_delivery_key(result.message.id)
        assert row.meta["input_source"] == INPUT_SOURCE_API
        assert row.meta["api_caller"] == "execution:agy|build"
        # The verified caller never leaks into the stored message metadata.
        assert result.message.meta == {}

        # An idempotent replay of the same append must not add a second copy.
        replay = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Вопрос через API",
            source=INPUT_SOURCE_API,
            external_id="q-1",
        )
        assert replay.replayed is False
        replay2 = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Вопрос через API",
            source=INPUT_SOURCE_API,
            external_id="q-1",
        )
        assert replay2.replayed is True
        assert len(repo.list_deliveries(conversation_id=conversation.id)) == 2
        assert (
            repo.get_delivery_by_idempotency_key(
                api_question_delivery_key(replay2.message.id)
            )
            is not None
        )
    finally:
        database.dispose()


def test_api_assistant_and_task_event_appends_are_not_questions(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation()
        conversations.append_message(
            conversation_id=conversation.id,
            role="assistant",
            text="Не вопрос",
            source=INPUT_SOURCE_API,
        )
        conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Системное событие",
            source=INPUT_SOURCE_TASK_EVENT,
        )
        assert TelegramDeliveryRepository(database).list_deliveries() == []
    finally:
        database.dispose()


def test_completed_turn_enqueues_reply_delivery(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation()
        question = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Вопрос",
        )
        processor = TurnProcessor(database)
        turn, reply = _claim_and_complete(
            processor, conversation.id, question.message.id
        )

        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(reply_delivery_key(turn.id))
        assert row is not None
        assert row.kind == DELIVERY_KIND_REPLY
        assert row.message_id == reply.id
        assert row.turn_id == turn.id
        assert row.meta["input_source"] == INPUT_SOURCE_API

        # Re-completion with the same token returns the stored reply and must
        # not enqueue a second delivery.
        assert processor.complete(turn.id, turn.claim_token, text="Готово").id == reply.id
        assert (
            len(
                TelegramDeliveryRepository(database).list_deliveries(
                    conversation_id=conversation.id
                )
            )
            == 2
        )
    finally:
        database.dispose()


def test_completed_telegram_turn_enqueues_reply_delivery(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation()
        question = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Вопрос из Telegram",
            source=INPUT_SOURCE_TELEGRAM,
        )
        processor = TurnProcessor(database)
        turn, _ = _claim_and_complete(
            processor, conversation.id, question.message.id
        )
        row = TelegramDeliveryRepository(database).get_delivery_by_idempotency_key(
            reply_delivery_key(turn.id)
        )
        assert row is not None
        assert row.meta["input_source"] == INPUT_SOURCE_TELEGRAM
    finally:
        database.dispose()


def test_completed_task_event_turn_does_not_enqueue_reply_delivery(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation()
        event = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Задача завершена",
            source=INPUT_SOURCE_TASK_EVENT,
        )
        processor = TurnProcessor(database)
        turn, _ = _claim_and_complete(processor, conversation.id, event.message.id)
        repo = TelegramDeliveryRepository(database)
        assert repo.get_delivery_by_idempotency_key(reply_delivery_key(turn.id)) is None
    finally:
        database.dispose()


def test_failed_api_turn_enqueues_error_delivery(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation()
        question = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Вопрос",
        )
        processor = TurnProcessor(database)
        processor.enqueue(
            conversation_id=conversation.id, input_message_id=question.message.id
        )
        turn = processor.claim_next(conversation_id=conversation.id)
        processor.fail(turn.id, turn.claim_token, "модель недоступна")

        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(turn_error_delivery_key(turn.id))
        assert row is not None
        assert row.kind == DELIVERY_KIND_TURN_ERROR
        assert row.meta["error"] == "модель недоступна"
        assert row.meta["input_source"] == INPUT_SOURCE_API
    finally:
        database.dispose()


def test_failed_telegram_turn_does_not_enqueue_error_delivery(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation()
        question = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Вопрос",
            source=INPUT_SOURCE_TELEGRAM,
        )
        processor = TurnProcessor(database)
        processor.enqueue(
            conversation_id=conversation.id, input_message_id=question.message.id
        )
        turn = processor.claim_next(conversation_id=conversation.id)
        processor.fail(turn.id, turn.claim_token, "ошибка")
        repo = TelegramDeliveryRepository(database)
        assert repo.get_delivery_by_idempotency_key(turn_error_delivery_key(turn.id)) is None
    finally:
        database.dispose()


def test_claim_next_pending_preserves_conversation_order(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        repo = TelegramDeliveryRepository(database)
        conv_a = conversations.create_conversation()
        conv_b = conversations.create_conversation()
        question_a = conversations.append_message(
            conversation_id=conv_a.id, role="user", text="QA"
        )
        question_b = conversations.append_message(
            conversation_id=conv_b.id, role="user", text="QB"
        )
        processor = TurnProcessor(database)
        turn_a, _ = _claim_and_complete(processor, conv_a.id, question_a.message.id)
        turn_b, _ = _claim_and_complete(processor, conv_b.id, question_b.message.id)
        # Conversation A now has question copy (earlier) and reply (later);
        # conversation B has one row.
        first = repo.claim_next_pending()
        assert first is not None and first.conversation_id == conv_a.id
        assert first.kind == DELIVERY_KIND_API_QUESTION

        # A is in sending: only B is claimable.
        second = repo.claim_next_pending()
        assert second is not None and second.conversation_id == conv_b.id
        # Nothing else pending outside sending conversations.
        assert repo.claim_next_pending() is None

        repo.mark_sent(second.id)
        repo.mark_sent(first.id)
        third = repo.claim_next_pending()
        assert third is not None and third.conversation_id == conv_a.id
        assert third.kind == DELIVERY_KIND_REPLY
        assert third.turn_id == turn_a.id
    finally:
        database.dispose()


def test_double_claim_does_not_duplicate_sending(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        repo = TelegramDeliveryRepository(database)
        conversation = conversations.create_conversation()
        question = conversations.append_message(
            conversation_id=conversation.id, role="user", text="Q"
        )
        row = repo.get_delivery_by_idempotency_key(
            api_question_delivery_key(question.message.id)
        )
        assert repo.claim_delivery(row.id) is True
        assert repo.claim_delivery(row.id) is False
        assert repo.claim_next_pending() is None
    finally:
        database.dispose()


def test_part_progress_and_stale_sending_recovery(migrated_db) -> None:
    database = _database(migrated_db)
    try:
        conversations = ConversationRepository(database)
        repo = TelegramDeliveryRepository(database)
        conversation = conversations.create_conversation()
        question = conversations.append_message(
            conversation_id=conversation.id, role="user", text="Q"
        )
        row = repo.get_delivery_by_idempotency_key(
            api_question_delivery_key(question.message.id)
        )
        assert repo.claim_delivery(row.id) is True
        repo.mark_part_sent(row.id, part_index=0, telegram_message_id=111)
        stored = repo.get_delivery_by_idempotency_key(row.idempotency_key)
        assert stored.parts_sent == 1
        assert stored.telegram_message_ids == [111]
        assert stored.status == DELIVERY_STATUS_SENDING

        # Simulate a crash between claiming and finishing: startup recovery
        # reopens the row, preserving per-part progress.
        assert repo.requeue_stale_sending() == 1
        stored = repo.get_delivery_by_idempotency_key(row.idempotency_key)
        assert stored.status == DELIVERY_STATUS_PENDING
        assert stored.parts_sent == 1

        assert repo.claim_delivery(row.id) is True
        repo.mark_part_sent(row.id, part_index=1, telegram_message_id=222)
        repo.mark_sent(row.id)
        stored = repo.get_delivery_by_idempotency_key(row.idempotency_key)
        assert stored.status == DELIVERY_STATUS_SENT
        assert stored.telegram_message_ids == [111, 222]
        assert stored.attempt_count == 2
    finally:
        database.dispose()


def test_links_and_deliveries_survive_restart(migrated_db) -> None:
    database = _database(migrated_db)
    conversations = ConversationRepository(database)
    conversation = conversations.create_conversation()
    question = conversations.append_message(
        conversation_id=conversation.id,
        role="user",
        text="Вопрос",
        source=INPUT_SOURCE_TELEGRAM,
        metadata={"chat_id": 42, "telegram_message_id": 900},
    )
    database.dispose()

    restarted = _database(migrated_db)
    try:
        repo = TelegramDeliveryRepository(restarted)
        link = repo.get_message_link(42, 900)
        assert link is not None
        assert link.conversation_id == conversation.id
        assert link.message_id == question.message.id
        assert repo.list_deliveries(conversation_id=conversation.id) == []
    finally:
        restarted.dispose()
