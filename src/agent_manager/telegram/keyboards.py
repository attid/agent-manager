"""Inline keyboards for the Telegram Secretary bot."""

from __future__ import annotations

from typing import Any
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def mode_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for selecting global mode when idle."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Новая задача", callback_data="action:new_task"),
                InlineKeyboardButton(text="💬 Разговор", callback_data="action:talk"),
            ],
            [
                InlineKeyboardButton(text="📋 Список задач", callback_data="action:list_tasks"),
            ],
        ]
    )


def talk_keyboard() -> InlineKeyboardMarkup:
    """Actions available during general talk mode."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Новый диалог", callback_data="action:reset_talk"),
                InlineKeyboardButton(text="📋 Список задач", callback_data="action:list_tasks"),
            ]
        ]
    )


def cancel_queue_keyboard(queue_id: str) -> InlineKeyboardMarkup:
    """Inline button to cancel a queued message while waiting."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"cancel_queue:{queue_id}",
                )
            ]
        ]
    )


def task_card_keyboard(task_id: str, status: str) -> InlineKeyboardMarkup:
    """Actions available for a selected task card."""
    buttons: list[list[InlineKeyboardButton]] = []

    action_row: list[InlineKeyboardButton] = []
    if status != "incoming":
        action_row.append(
            InlineKeyboardButton(
                text="📥 Во входящие", callback_data=f"set_status:{task_id}:incoming"
            )
        )
    if status != "in_progress":
        action_row.append(
            InlineKeyboardButton(
                text="⚙️ В работу", callback_data=f"set_status:{task_id}:in_progress"
            )
        )
    if status != "done":
        action_row.append(
            InlineKeyboardButton(
                text="✅ Завершить", callback_data=f"set_status:{task_id}:done"
            )
        )
    if status != "archived":
        action_row.append(
            InlineKeyboardButton(
                text="📦 В архив", callback_data=f"set_status:{task_id}:archived"
            )
        )
    if action_row:
        # Split action buttons into rows of max 2 buttons
        buttons.extend([action_row[i : i + 2] for i in range(0, len(action_row), 2)])

    talk_label = (
        "✍️ Ответить на уточнение"
        if status == "waiting_for_input"
        else "💬 Обсудить с секретарём"
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=talk_label, callback_data=f"talk_task:{task_id}"
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="📋 К списку задач", callback_data="action:list_tasks"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def task_categories_keyboard(
    active_category: str = "incoming",
    tasks: list[dict[str, Any]] | None = None,
    page: int = 1,
    page_size: int = 8,
) -> InlineKeyboardMarkup:
    """Category tabs and paginated task list buttons."""
    categories = [
        ("incoming", "📥 Входящие"),
        ("in_progress", "⚙️ В работе"),
        ("waiting_for_input", "⚠️ Уточнение"),
        ("done", "✅ Завершённые"),
        ("archived", "📦 Архив"),
    ]

    # Category tabs
    cat_buttons = [
        InlineKeyboardButton(
            text=f"• {label} •" if cat == active_category else label,
            callback_data=f"cat:{cat}:1",
        )
        for cat, label in categories
    ]
    # Split into 2 rows (3 and 2)
    keyboard = [cat_buttons[:3], cat_buttons[3:]]

    all_tasks = tasks or []
    total_count = len(all_tasks)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * page_size
    page_tasks = all_tasks[start_idx : start_idx + page_size]

    # Tasks on current page
    if page_tasks:
        for t in page_tasks:
            title = t.get("title") or t["id"]
            if len(title) > 30:
                title = title[:27] + "..."
            icon = "⚠️" if t.get("status") == "waiting_for_input" else "📌"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {title}",
                        callback_data=f"select_task:{t['id']}",
                    )
                ]
            )
    else:
        keyboard.append(
            [InlineKeyboardButton(text="— Нет задач в категории —", callback_data="noop")]
        )

    # Pagination navigation if more than 1 page
    if total_pages > 1:
        prev_btn = (
            InlineKeyboardButton(
                text="⬅️", callback_data=f"cat:{active_category}:{current_page - 1}"
            )
            if current_page > 1
            else InlineKeyboardButton(text="⏹", callback_data="noop")
        )
        page_btn = InlineKeyboardButton(
            text=f"Стр. {current_page} из {total_pages}", callback_data="noop"
        )
        next_btn = (
            InlineKeyboardButton(
                text="➡️", callback_data=f"cat:{active_category}:{current_page + 1}"
            )
            if current_page < total_pages
            else InlineKeyboardButton(text="⏹", callback_data="noop")
        )
        keyboard.append([prev_btn, page_btn, next_btn])

    # Global actions
    keyboard.append(
        [
            InlineKeyboardButton(text="➕ Новая задача", callback_data="action:new_task"),
            InlineKeyboardButton(text="💬 Общий разговор", callback_data="action:talk"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)
