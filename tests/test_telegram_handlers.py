"""Focused tests for the Telegram task-context handlers."""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Chat, Message, User

from agent_manager.telegram.config import TelegramConfig
from agent_manager.telegram.handlers import on_talk_task, on_user_message
from agent_manager.telegram.state import UserStateStorage
from agent_manager.telegram.worker import QueueProcessor


def _user(user_id: int = 12345) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def _message(*, text: str, user: User) -> Message:
    message = MagicMock(spec=Message)
    message.message_id = 1
    message.from_user = user
    message.chat = Chat(id=user.id, type="private")
    message.text = text
    message.voice = None
    message.photo = None
    message.document = None
    message.caption = None
    message.answer = AsyncMock()
    return message


def _callback(data: str, *, user: User) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = user
    callback.message = _message(text="", user=user)
    callback.answer = AsyncMock()
    return callback


def test_talk_task_binds_the_existing_task_conversation(
    tmp_path: pathlib.Path,
) -> None:
    """The discuss-with-secretary action binds the task's conversation."""
    user = _user()
    callback = _callback("talk_task:task-123", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    client = MagicMock()
    client.get_task = AsyncMock(
        return_value={
            "id": "task-123",
            "number": 42,
            "title": "Проверить деплой",
            "metadata": {"conversation_id": "conv-123"},
        }
    )

    asyncio.run(
        on_talk_task(callback, storage=storage, client=client)
    )

    state = storage.get_user_state(user.id)
    assert state.mode == "task"
    assert state.active_task_id == "task-123"
    assert state.active_conversation_id == "conv-123"
    prompt = callback.message.answer.call_args.args[0]
    assert "Проверить деплой" in prompt


def test_task_mode_message_is_forwarded_with_task_id(
    tmp_path: pathlib.Path,
) -> None:
    """The handler keeps context_task_id so the worker can enrich the turn."""
    user = _user()
    message = _message(text="че там с задачей?", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_user_mode(
        user.id, "task", task_id="task-123", conversation_id="conv-123"
    )
    client = MagicMock()
    client.get_task = AsyncMock(
        return_value={
            "id": "task-123",
            "number": 42,
            "title": "Проверить деплой",
            "status": "in_progress",
            "metadata": {"conversation_id": "conv-123"},
        }
    )
    processor = MagicMock(spec=QueueProcessor)
    processor.submit_message = AsyncMock()
    config = TelegramConfig(
        bot_token="fake", allowed_user_id=user.id, media_dir=tmp_path
    )

    asyncio.run(
        on_user_message(
            message,
            storage=storage,
            client=client,
            queue_processor=processor,
            config=config,
        )
    )

    processor.submit_message.assert_awaited_once()
    kwargs = processor.submit_message.call_args.kwargs
    assert kwargs["context_mode"] == "task"
    assert kwargs["context_task_id"] == "task-123"
    assert kwargs["context_conversation_id"] == "conv-123"
    assert kwargs["text"] == "че там с задачей?"
