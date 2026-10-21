"""Unified Telegram messenger: rendering, sending and the durable outbox."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from agent_manager.storage import ConversationRepository, Database, TaskRepository
from agent_manager.storage.telegram_delivery import (
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENT,
    TelegramDeliveryRepository,
    reply_delivery_key,
)
from agent_manager.secretary import TurnProcessor
from agent_manager.telegram.keyboards import dialog_switch_keyboard
from agent_manager.telegram.messenger import (
    TelegramDeliveryService,
    api_question_header,
    compose_dialog_message,
    conversation_hashtag,
    format_verified_caller,
    render_foreign_context_plate,
    split_for_telegram,
    with_conversation_footer,
)


def _bot(message_id: int = 501) -> AsyncMock:
    bot = AsyncMock()
    bot.send_rich_message.return_value.message_id = message_id
    return bot


def _service(migrated_db, bot, **kwargs) -> TelegramDeliveryService:
    return TelegramDeliveryService(
        bot=bot,
        database=Database.from_path(migrated_db),
        claim_grace_seconds=0.0,
        **kwargs,
    )


def test_format_verified_caller_only_trusts_principal_shapes() -> None:
    assert format_verified_caller(None) is None
    assert format_verified_caller("admin") == "администратор"
    assert format_verified_caller("execution:agy|build") == "агент: agy"
    assert format_verified_caller("execution:solo") == "агент: solo"
    # Free-form junk never renders as a confirmed identity.
    assert format_verified_caller("random string") is None


def test_api_question_header_variants() -> None:
    assert api_question_header() == "📨 Вопрос секретарю через API"
    assert (
        api_question_header("execution:agy|build")
        == "📨 Вопрос секретарю через API (агент: agy)"
    )
    assert (
        api_question_header("admin")
        == "📨 Вопрос секретарю через API (администратор)"
    )


def test_foreign_context_plate_snapshot() -> None:
    assert render_foreign_context_plate() is None
    assert (
        render_foreign_context_plate(active_task_number=41)
        == "↩️ Другой диалог · сейчас активна задача №41"
    )
    assert (
        render_foreign_context_plate(active_conversation_id="abc")
        == "↩️ Другой диалог"
    )


def test_split_for_telegram_footers_every_part() -> None:
    conversation_id = "a" * 32
    composed = compose_dialog_message(
        header="📨 Вопрос секретарю через API",
        body="т" * 9000,
    )
    parts = split_for_telegram(composed, conversation_id)
    assert len(parts) == 3
    assert all(len(part) <= 4096 for part in parts)
    assert all(part.endswith(conversation_hashtag(conversation_id)) for part in parts)
    assert "".join(
        part[: -len(conversation_hashtag(conversation_id))].rstrip() for part in parts
    ).endswith("т")


def test_deliver_api_question_copy_with_uuid_and_button(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 77})
        question = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Что с релизом?",
            verified_caller="execution:agy|build",
        )
        bot = _bot(501)
        service = _service(migrated_db, bot)
        assert asyncio.run(service.deliver_pending()) == 1

        bot.send_rich_message.assert_awaited_once()
        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs["chat_id"] == 77
        assert kwargs["rich_message"].markdown == (
            "📨 Вопрос секретарю через API (агент: agy)\n\n"
            f"Что с релизом?\n\n#{conversation.id}"
        )
        assert kwargs["reply_markup"] == dialog_switch_keyboard(
            conversation_id=conversation.id
        )

        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == DELIVERY_STATUS_SENT
        link = repo.get_message_link(77, 501)
        assert link is not None
        assert link.conversation_id == conversation.id
        assert link.message_id == question.message.id
        assert link.origin == "bot"
    finally:
        database.dispose()


def test_task_bound_conversation_gets_switch_task_button(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 88})
        task = tasks.create(
            title="Платёжный модуль",
            status="in_progress",
            metadata={"conversation_id": conversation.id},
        )
        conversations.append_message(
            conversation_id=conversation.id, role="user", text="Как дела?"
        )
        bot = _bot(601)
        service = _service(migrated_db, bot)
        assert asyncio.run(service.deliver_pending()) == 1
        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs["reply_markup"] == dialog_switch_keyboard(
            conversation_id=conversation.id,
            task_id=task.id,
            task_number=task.number,
        )
    finally:
        database.dispose()


def test_reply_row_header_for_api_input_and_plain_for_telegram(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        processor = TurnProcessor(database)
        service = _service(migrated_db, _bot(701))

        api_conv = conversations.create_conversation(metadata={"chat_id": 1})
        q1 = conversations.append_message(
            conversation_id=api_conv.id, role="user", text="API вопрос"
        )
        processor.enqueue(conversation_id=api_conv.id, input_message_id=q1.message.id)
        turn1 = processor.claim_next(conversation_id=api_conv.id)
        processor.complete(turn1.id, turn1.claim_token, text="API ответ")

        tg_conv = conversations.create_conversation(metadata={"chat_id": 2})
        q2 = conversations.append_message(
            conversation_id=tg_conv.id,
            role="user",
            text="TG вопрос",
            source="telegram",
        )
        processor.enqueue(conversation_id=tg_conv.id, input_message_id=q2.message.id)
        turn2 = processor.claim_next(conversation_id=tg_conv.id)
        processor.complete(turn2.id, turn2.claim_token, text="TG ответ")

        # Three delivery rows: the API question copy, its reply, and the
        # telegram-originated reply (the telegram question itself is already
        # in the chat and is never copied).
        assert asyncio.run(service.deliver_pending()) == 3
        texts = {
            call.kwargs["rich_message"].markdown
            for call in service.bot.send_rich_message.await_args_list
        }
        # The API question copy and reply keep their distinguishing headers;
        # the telegram-originated reply stays a bare answer.
        assert any(t.startswith("📨 Вопрос секретарю через API") for t in texts)
        assert (
            f"💬 Ответ секретаря (вопрос был через API)\n\nAPI ответ\n\n#{api_conv.id}"
            in texts
        )
        assert f"TG ответ\n\n#{tg_conv.id}" in texts
    finally:
        database.dispose()


def test_no_recipient_keeps_row_pending(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={})
        question = conversations.append_message(
            conversation_id=conversation.id, role="user", text="Куда доставлять?"
        )
        bot = _bot()
        service = _service(migrated_db, bot)
        # The row is claimed, diagnosed and released — nothing counts as
        # delivered while the recipient is unknown.
        assert asyncio.run(service.deliver_pending()) == 0
        bot.send_rich_message.assert_not_awaited()
        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == DELIVERY_STATUS_PENDING
    finally:
        database.dispose()


def test_send_error_with_unknown_outcome_requeues(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 99})
        question = conversations.append_message(
            conversation_id=conversation.id, role="user", text="Вопрос"
        )
        bot = _bot()
        for method in (bot.send_rich_message, bot.send_message, bot.send_document):
            method.side_effect = RuntimeError("network timeout")
        service = _service(migrated_db, bot)
        # Attempted with an unknown outcome: released back to pending, the
        # pass stops instead of spinning on the same row.
        assert asyncio.run(service.deliver_pending()) == 0
        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == DELIVERY_STATUS_PENDING

        # The retry after an "unknown outcome" may resend the same part —
        # at-least-once by design — and then completes.
        for method in (bot.send_rich_message, bot.send_message, bot.send_document):
            method.side_effect = None
        bot.send_rich_message.return_value.message_id = 801
        assert asyncio.run(service.deliver_pending()) == 1
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == DELIVERY_STATUS_SENT
    finally:
        database.dispose()


def test_definite_telegram_rejection_marks_failed(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 99})
        question = conversations.append_message(
            conversation_id=conversation.id, role="user", text="Вопрос"
        )
        bot = _bot()
        error = TelegramBadRequest(method="sendRichMessage", message="chat not found")
        for method in (bot.send_rich_message, bot.send_message, bot.send_document):
            method.side_effect = error
        service = _service(migrated_db, bot)
        assert asyncio.run(service.deliver_pending()) == 1
        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(f"tg-q:{question.message.id}")
        assert row.status == "failed"
        assert "chat not found" in row.error
    finally:
        database.dispose()


def test_foreign_plate_rendered_when_active_context_differs(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 55})
        conversations.append_message(
            conversation_id=conversation.id, role="user", text="Фоновый вопрос"
        )
        bot = _bot(901)
        service = _service(
            migrated_db,
            bot,
            active_context_probe=lambda: {
                "mode": "task",
                "conversation_id": "other",
                "task_number": 41,
            },
        )
        assert asyncio.run(service.deliver_pending()) == 1
        markdown = bot.send_rich_message.await_args.kwargs["rich_message"].markdown
        assert "↩️ Другой диалог · сейчас активна задача №41" in markdown
        assert markdown.endswith(f"#{conversation.id}")
    finally:
        database.dispose()


def test_claim_grace_defers_fresh_rows_to_synchronous_worker(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 66})
        conversations.append_message(
            conversation_id=conversation.id, role="user", text="Свежий вопрос"
        )
        bot = _bot()
        service = TelegramDeliveryService(
            bot=bot,
            database=database,
            claim_grace_seconds=60.0,
        )
        assert asyncio.run(service.deliver_pending()) == 0
        bot.send_rich_message.assert_not_awaited()
    finally:
        database.dispose()


def test_worker_and_outbox_cannot_double_send_a_reply(migrated_db) -> None:
    database = Database.from_path(migrated_db)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.create_conversation(metadata={"chat_id": 33})
        question = conversations.append_message(
            conversation_id=conversation.id,
            role="user",
            text="Вопрос",
            source="telegram",
        )
        processor = TurnProcessor(database)
        processor.enqueue(
            conversation_id=conversation.id, input_message_id=question.message.id
        )
        turn = processor.claim_next(conversation_id=conversation.id)
        processor.complete(turn.id, turn.claim_token, text="Ответ")

        bot = _bot()
        service = _service(migrated_db, bot)
        first = service.claim_reply_delivery(turn.id)
        assert first is not None
        # The outbox (and any second worker) loses the claim and must not send.
        assert service.claim_reply_delivery(turn.id) is None
        assert asyncio.run(service.deliver_pending()) == 0
        bot.send_rich_message.assert_not_awaited()

        service.record_delivery_sent(
            first, chat_id=33, telegram_message_ids=[444, 445]
        )
        repo = TelegramDeliveryRepository(database)
        row = repo.get_delivery_by_idempotency_key(reply_delivery_key(turn.id))
        assert row.status == DELIVERY_STATUS_SENT
        assert row.telegram_message_ids == [444, 445]
        assert repo.get_message_link(33, 444).origin == "bot"
        assert repo.get_message_link(33, 445).origin == "bot"
    finally:
        database.dispose()
