"""Unit tests for the Telegram queue worker's task-context enrichment."""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

from kuzya.telegram.state import UserStateStorage
from kuzya.telegram.transcription import DeepgramTranscriber
from kuzya.telegram.worker import QueueProcessor


def _context_processor(
    client: MagicMock,
    storage: UserStateStorage,
    *,
    bot: MagicMock | None = None,
) -> QueueProcessor:
    bot = bot or MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_rich_message = AsyncMock()
    bot.send_document = AsyncMock()
    return QueueProcessor(
        bot=bot,
        backend_client=client,
        storage=storage,
        transcriber=DeepgramTranscriber(mock_transcript="тест"),
        poll_interval_seconds=0.001,
        turn_timeout_seconds=1.0,
    )


def _completed_client() -> MagicMock:
    client = MagicMock()
    client.append_message = AsyncMock(return_value={"id": "message-1"})
    client.create_turn = AsyncMock(return_value={"id": "turn-1"})
    client.get_turn = AsyncMock(
        return_value={"status": "completed", "reply_message_id": "reply-1"}
    )
    client.list_messages = AsyncMock(
        return_value=[{"id": "reply-1", "text": "Ответ секретаря"}]
    )
    return client


def _queued_task_message(
    storage: UserStateStorage, *, text: str
) -> object:
    return storage.enqueue_message(
        user_id=12345,
        chat_id=12345,
        telegram_message_id=10,
        context_mode="task",
        context_task_id="task-123",
        context_conversation_id="conv-1",
        text=text,
    )


def test_task_context_message_gets_header_and_metadata(
    tmp_path: pathlib.Path,
) -> None:
    """The worker stamps the task identity onto text and message metadata."""
    storage = UserStateStorage(tmp_path / "state.db")
    client = _completed_client()
    client.get_task = AsyncMock(
        return_value={
            "id": "task-123",
            "number": 42,
            "title": "Проверить деплой",
        }
    )
    processor = _context_processor(client, storage)
    queued = _queued_task_message(
        storage, text="че там с задачей?"
    )

    asyncio.run(processor.process_message(queued))

    client.get_task.assert_awaited_once_with("task-123")
    args, kwargs = client.append_message.await_args
    assert args == ("conv-1",)
    expected_header = (
        "[Контекст задачи #42: «Проверить деплой» (id: task-123)]"
    )
    assert kwargs["text"] == f"{expected_header}\n\nче там с задачей?"
    assert kwargs["metadata"]["task_id"] == "task-123"
    assert kwargs["metadata"]["task_number"] == 42
    assert kwargs["metadata"]["task_title"] == "Проверить деплой"
    # The existing media/telegram metadata is preserved.
    assert kwargs["metadata"]["telegram_message_id"] == 10
    assert kwargs["metadata"]["media_type"] == "text"


def test_existing_task_header_is_not_duplicated(
    tmp_path: pathlib.Path,
) -> None:
    """A retried or prefixed message keeps exactly one task context marker."""
    storage = UserStateStorage(tmp_path / "state.db")
    client = _completed_client()
    client.get_task = AsyncMock(
        return_value={
            "id": "task-123",
            "number": 42,
            "title": "Проверить деплой",
        }
    )
    processor = _context_processor(client, storage)
    header = (
        "[Контекст задачи #42: «Проверить деплой» (id: task-123)]"
    )
    queued = _queued_task_message(
        storage, text=f"{header}\n\nче там с задачей?"
    )

    asyncio.run(processor.process_message(queued))

    _, kwargs = client.append_message.await_args
    assert kwargs["text"].count(header) == 1
    assert kwargs["text"] == f"{header}\n\nче там с задачей?"


def test_talk_message_is_not_enriched_with_task_context(
    tmp_path: pathlib.Path,
) -> None:
    """Only task-bound conversations pay the task lookup and get metadata."""
    storage = UserStateStorage(tmp_path / "state.db")
    client = _completed_client()
    client.get_task = AsyncMock()
    processor = _context_processor(client, storage)
    queued = storage.enqueue_message(
        user_id=12345,
        chat_id=12345,
        telegram_message_id=11,
        context_mode="talk",
        context_task_id=None,
        context_conversation_id="conv-talk",
        text="общий вопрос",
    )

    asyncio.run(processor.process_message(queued))

    client.get_task.assert_not_called()
    _, kwargs = client.append_message.await_args
    assert kwargs["text"] == "общий вопрос"
    assert "task_id" not in kwargs["metadata"]
    assert "task_number" not in kwargs["metadata"]
    assert "task_title" not in kwargs["metadata"]


def test_agent_talk_reply_has_end_and_secretary_buttons(
    tmp_path: pathlib.Path,
) -> None:
    """A direct-agent reply keeps its own finish/secretary controls."""
    storage = UserStateStorage(tmp_path / "state.db")
    client = _completed_client()
    client.get_conversation = AsyncMock(
        return_value={
            "id": "conv-agent",
            "metadata": {"talkable_agent": "devops-docker3"},
        }
    )
    processor = _context_processor(client, storage)
    queued = storage.enqueue_message(
        user_id=12345,
        chat_id=12345,
        telegram_message_id=12,
        context_mode="agent_talk",
        context_task_id=None,
        context_conversation_id="conv-agent",
        text="проверь nginx",
    )

    asyncio.run(processor.process_message(queued))

    assert client.get_conversation.await_count == 2
    client.get_conversation.assert_awaited_with("conv-agent")
    client.get_task.assert_not_called()
    _, kwargs = client.append_message.await_args
    assert "task_id" not in kwargs["metadata"]
    markup = processor.bot.send_rich_message.await_args.kwargs[
        "reply_markup"
    ]
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "end_talk:conv-agent" in callbacks
    assert "action:talk" in callbacks
