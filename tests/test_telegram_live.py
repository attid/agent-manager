"""Telegram live monitor command, callback and keyboard tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Chat, Message, User

from kuzya.telegram.handlers import cmd_live, format_execution_monitor, on_action_live
from kuzya.telegram.keyboards import (
    execution_monitor_keyboard,
    task_categories_keyboard,
)


def _user() -> User:
    return User(id=12345, is_bot=False, first_name="Test")


def _message(*, user: User) -> Message:
    message = MagicMock(spec=Message)
    message.message_id = 1
    message.from_user = user
    message.chat = Chat(id=user.id, type="private")
    message.answer = AsyncMock()
    return message


def _callback(data: str, *, user: User) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = user
    callback.message = _message(user=user)
    callback.answer = AsyncMock()
    return callback


def _payload(*, tools: int) -> dict:
    return {
        "generated_at": "2026-01-01T12:34:56Z",
        "running": [
            {
                "kind": "analysis",
                "entry_type": "operation",
                "operation_kind": "analysis",
                "kind_label": "Анализ",
                "target_number": 76,
                "target_title": "Проверить деплой",
                "agent_id": "analyzer",
                "elapsed": 360,
                "session_available": True,
                "tools_used": tools,
                "tokens": {"totalTokens": 138000},
                "cost": 0.13,
                "last_activity_sec": 3,
                "current_tool": "bash",
            }
        ],
        "queues": {
            "turns": 2,
            "operations": 1,
            "subscriptions": 0,
            "tasks": 1,
        },
    }


def test_category_keyboard_has_live_button() -> None:
    keyboard = task_categories_keyboard()
    live_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data == "action:live"
    ]
    assert [button.text for button in live_buttons] == ["⚡ Исполнители"]


def test_refresh_keyboard_reuses_the_live_callback() -> None:
    keyboard = execution_monitor_keyboard()
    [row] = keyboard.inline_keyboard
    [button] = row
    assert button.text == "🔄 Обновить"
    assert button.callback_data == "action:live"


def test_monitor_line_contains_live_session_stats() -> None:
    text = format_execution_monitor(_payload(tools=11))
    assert (
        "#76 · Анализ · агент analyzer · 6 мин · 11 инструментов · "
        "138k токенов ($0.13) · активность 3с назад · сейчас: bash"
    ) in text
    assert "завис" not in text.lower()
    assert "В очереди" in text


def test_callback_redraws_the_same_message_with_fresh_numbers() -> None:
    user = _user()
    first = _callback("action:live", user=user)
    second = _callback("action:live", user=user)
    client = MagicMock()
    client.get_execution_monitor = AsyncMock(
        side_effect=[_payload(tools=11), _payload(tools=12)]
    )

    with patch(
        "kuzya.telegram.handlers.rich_edit_message", new_callable=AsyncMock
    ) as edit:
        asyncio.run(on_action_live(first, client=client))
        asyncio.run(on_action_live(second, client=client))

    assert edit.await_count == 2
    first_call, second_call = edit.await_args_list
    assert "11 инструментов" in first_call.args[1]
    assert "12 инструментов" in second_call.args[1]
    for call in (first_call, second_call):
        keyboard = call.kwargs["reply_markup"]
        [row] = keyboard.inline_keyboard
        assert row[0].callback_data == "action:live"
    first.answer.assert_awaited_with("Обновлено")
    second.answer.assert_awaited_with("Обновлено")


def test_live_command_sends_the_monitor_with_refresh_button() -> None:
    user = _user()
    message = _message(user=user)
    client = MagicMock()
    client.get_execution_monitor = AsyncMock(return_value=_payload(tools=11))

    asyncio.run(cmd_live(message, client=client))

    message.answer.assert_awaited_once()
    text = message.answer.call_args.args[0]
    assert "11 инструментов" in text
    keyboard = message.answer.call_args.kwargs["reply_markup"]
    [row] = keyboard.inline_keyboard
    assert row[0].callback_data == "action:live"
