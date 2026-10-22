"""Telegram direct conversations with talkable task agents."""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Chat, Message, User

from kuzya.telegram.config import TelegramConfig
from kuzya.telegram.handlers import (
    on_category_switch,
    on_end_talk,
    on_select_talk_agent,
    on_talk_agent_list,
    on_user_message,
)
from kuzya.telegram.state import UserStateStorage
from kuzya.telegram.worker import QueueProcessor


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
    message.reply_to_message = None
    message.answer = AsyncMock()
    return message


def _callback(data: str, *, user: User) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = user
    callback.message = _message(text="", user=user)
    callback.message.edit_reply_markup = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _talkable_agent(
    agent_id: str = "devops-docker3",
    *,
    name: str = "DevOps Docker",
    project: str = "devops-workspace",
) -> dict:
    return {
        "agent_id": agent_id,
        "name": name,
        "runner_config": {"talkable": True, "project": project},
    }


def test_talk_agent_list_hides_agents_without_flag() -> None:
    user = _user()
    callback = _callback("talk_agent:1", user=user)
    client = MagicMock()
    client.list_agents = AsyncMock(
        return_value=[
            _talkable_agent(),
            {
                "agent_id": "quiet",
                "name": "Quiet",
                "runner_config": {"project": "quiet"},
            },
        ]
    )

    asyncio.run(on_talk_agent_list(callback, client=client))

    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "select_talk_agent:devops-docker3" in callbacks
    assert "select_talk_agent:quiet" not in callbacks


def test_select_agent_creates_locked_conversation(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    callback = _callback("select_talk_agent:devops-docker3", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    client = MagicMock()
    client.get_agent = AsyncMock(return_value=_talkable_agent())
    client.list_conversations = AsyncMock(return_value=[])
    client.create_conversation = AsyncMock(
        return_value={"id": "conv-agent", "metadata": {}}
    )
    client.start_talk = AsyncMock(
        return_value={
            "id": "conv-agent",
            "metadata": {
                "talkable_agent": "devops-docker3",
                "talk_active": True,
            },
        }
    )

    asyncio.run(
        on_select_talk_agent(
            callback, storage=storage, client=client
        )
    )

    client.create_conversation.assert_awaited_once()
    _, kwargs = client.create_conversation.await_args
    assert kwargs["metadata"]["talkable_agent"] == "devops-docker3"
    assert kwargs["metadata"]["talk_workspace"] == "devops-workspace"
    assert kwargs["metadata"]["origin"] == "telegram_agent_talk"
    client.start_talk.assert_awaited_once_with("conv-agent")
    state = storage.get_user_state(user.id)
    assert state.mode == "agent_talk"
    assert state.talk_agent_conversation_id == "conv-agent"
    assert state.active_conversation_id == "conv-agent"
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "end_talk:conv-agent" in callbacks
    assert "action:talk" in callbacks


def test_select_agent_resumes_the_same_conversation(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    callback = _callback("select_talk_agent:devops-docker3", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    client = MagicMock()
    client.get_agent = AsyncMock(return_value=_talkable_agent())
    existing = {
        "id": "conv-old",
        "metadata": {
            "origin": "telegram_agent_talk",
            "user_id": user.id,
            "talkable_agent": "devops-docker3",
            "talk_active": False,
            "talk_started_at": "2026-01-01T10:00:00+00:00",
        },
    }
    client.list_conversations = AsyncMock(return_value=[existing])
    client.start_talk = AsyncMock(
        return_value={
            **existing,
            "metadata": {**existing["metadata"], "talk_active": True},
        }
    )
    client.create_conversation = AsyncMock()

    asyncio.run(
        on_select_talk_agent(
            callback, storage=storage, client=client
        )
    )

    client.create_conversation.assert_not_awaited()
    client.start_talk.assert_awaited_once_with("conv-old")
    assert storage.get_user_state(
        user.id
    ).talk_agent_conversation_id == "conv-old"


def test_select_agent_rejects_profile_without_flag(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    callback = _callback("select_talk_agent:quiet", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    client = MagicMock()
    client.get_agent = AsyncMock(
        return_value={
            "agent_id": "quiet",
            "name": "Quiet",
            "runner_config": {},
        }
    )
    client.create_conversation = AsyncMock()

    asyncio.run(
        on_select_talk_agent(
            callback, storage=storage, client=client
        )
    )

    client.create_conversation.assert_not_awaited()
    assert storage.get_user_state(user.id).mode == "idle"


def test_user_message_routes_to_agent_conversation(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    message = _message(text="проверь nginx", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_talk_agent_conversation(user.id, "conv-agent")
    client = MagicMock()
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

    _, kwargs = processor.submit_message.await_args
    assert kwargs["context_mode"] == "agent_talk"
    assert kwargs["context_conversation_id"] == "conv-agent"
    assert kwargs["context_task_id"] is None
    assert kwargs["text"] == "проверь nginx"


def test_end_talk_releases_the_workspace_lock(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    callback = _callback("end_talk:conv-agent", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_talk_agent_conversation(user.id, "conv-agent")
    client = MagicMock()
    active = {
        "id": "conv-agent",
        "metadata": {
            "talkable_agent": "devops-docker3",
            "talk_active": True,
        },
    }
    client.get_conversation = AsyncMock(return_value=active)
    client.request_end_talk = AsyncMock(
        return_value={
            "id": "conv-agent",
            "metadata": {
                "talkable_agent": "devops-docker3",
                "talk_active": True,
                "talk_closing": True,
            },
        }
    )
    client.finalize_talk = AsyncMock(
        return_value={
            "id": "conv-agent",
            "metadata": {
                "talkable_agent": "devops-docker3",
                "talk_active": False,
                "talk_closed": True,
            },
        }
    )

    asyncio.run(
        on_end_talk(callback, storage=storage, client=client)
    )

    client.request_end_talk.assert_awaited_once_with("conv-agent")
    client.finalize_talk.assert_awaited_once_with("conv-agent")
    state = storage.get_user_state(user.id)
    assert state.mode == "talk"
    assert state.talk_agent_conversation_id is None
    text = callback.message.answer.await_args.args[0]
    assert "Workspace освобождён" in text


def test_category_tabs_report_active_talk_count() -> None:
    user = _user()
    callback = _callback("cat:incoming:1", user=user)
    client = MagicMock()
    client.list_tasks = AsyncMock(return_value=[])
    client.get_task_counts = AsyncMock(return_value={})
    client.list_conversations = AsyncMock(
        return_value=[
            {
                "id": "conv-agent",
                "metadata": {
                    "talkable_agent": "devops-docker3",
                    "talk_active": True,
                },
            }
        ]
    )

    asyncio.run(on_category_switch(callback, client=client))

    markup = callback.message.edit_reply_markup.await_args.kwargs[
        "reply_markup"
    ]
    talks = [
        button
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data == "action:active_talks"
    ]
    assert [button.text for button in talks] == ["🗣 Разговоры: 1"]


def test_select_agent_waits_when_workspace_is_busy(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    callback = _callback("select_talk_agent:devops-docker3", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    client = MagicMock()
    client.get_agent = AsyncMock(return_value=_talkable_agent())
    client.list_conversations = AsyncMock(return_value=[])
    client.create_conversation = AsyncMock(
        return_value={"id": "conv-agent", "metadata": {}}
    )
    client.start_talk = AsyncMock(
        return_value={
            "id": "conv-agent",
            "metadata": {
                "talkable_agent": "devops-docker3",
                "talk_active": False,
                "talk_pending": True,
            },
        }
    )

    asyncio.run(
        on_select_talk_agent(
            callback, storage=storage, client=client
        )
    )

    state = storage.get_user_state(user.id)
    assert state.mode == "agent_talk"
    assert state.talk_agent_conversation_id == "conv-agent"
    text = callback.message.answer.await_args.args[0]
    assert "Workspace занят" in text
    assert "Первое сообщение будет ждать" in text


def test_end_talk_keeps_lock_while_a_local_message_is_processing(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    callback = _callback("end_talk:conv-agent", user=user)
    storage = UserStateStorage(tmp_path / "state.db")
    storage.set_talk_agent_conversation(user.id, "conv-agent")
    processing = storage.enqueue_message(
        user_id=user.id,
        chat_id=user.id,
        telegram_message_id=10,
        context_mode="agent_talk",
        context_task_id=None,
        context_conversation_id="conv-agent",
        text="проверь nginx",
    )
    storage.set_message_status(processing.id, "processing")
    waiting = storage.enqueue_message(
        user_id=user.id,
        chat_id=user.id,
        telegram_message_id=11,
        context_mode="agent_talk",
        context_task_id=None,
        context_conversation_id="conv-agent",
        text="и логи",
    )

    client = MagicMock()
    client.get_conversation = AsyncMock(
        return_value={
            "id": "conv-agent",
            "metadata": {
                "talkable_agent": "devops-docker3",
                "talk_active": True,
            },
        }
    )
    client.request_end_talk = AsyncMock(
        return_value={
            "id": "conv-agent",
            "metadata": {
                "talkable_agent": "devops-docker3",
                "talk_active": True,
                "talk_closing": True,
            },
        }
    )
    client.finalize_talk = AsyncMock()

    asyncio.run(on_end_talk(callback, storage=storage, client=client))

    client.request_end_talk.assert_awaited_once_with("conv-agent")
    client.finalize_talk.assert_not_awaited()
    assert (
        storage.get_queued_message(waiting.id).status == "cancelled"
    )
    assert (
        storage.get_queued_message(processing.id).status == "processing"
    )
    state = storage.get_user_state(user.id)
    assert state.mode == "talk"
    text = callback.message.answer.await_args.args[0]
    assert "закрывается" in text
    assert "Workspace освободится автоматически" in text


def test_reply_to_ended_agent_talk_is_not_enqueued(
    tmp_path: pathlib.Path,
) -> None:
    user = _user()
    message = _message(text="проверь ещё раз", user=user)
    replied = MagicMock(spec=Message)
    replied.message_id = 99
    replied.from_user = user
    message.reply_to_message = replied
    storage = UserStateStorage(tmp_path / "state.db")
    client = MagicMock()
    client.get_telegram_link = AsyncMock(
        return_value={
            "conversation_id": "conv-agent",
            "task_id": None,
        }
    )
    client.get_conversation = AsyncMock(
        return_value={
            "id": "conv-agent",
            "metadata": {
                "talkable_agent": "devops-docker3",
                "talk_active": False,
                "talk_closed": True,
            },
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

    processor.submit_message.assert_not_awaited()
    text = message.answer.await_args.args[0]
    assert "завершён" in text
    assert "Новый ход не отправлен" in text
