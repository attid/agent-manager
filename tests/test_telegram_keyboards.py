"""Tests for Telegram task-category counters and active-tab highlighting."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from kuzya.telegram.keyboards import task_categories_keyboard


def _tabs(keyboard: InlineKeyboardMarkup) -> dict[str, str]:
    tabs: dict[str, str] = {}
    for row in keyboard.inline_keyboard:
        for button in row:
            if button.callback_data and button.callback_data.startswith("cat:"):
                category = button.callback_data.split(":")[1]
                tabs[category] = button.text
    return tabs


def test_category_tabs_show_counts_except_archive() -> None:
    keyboard = task_categories_keyboard(
        counts={
            "incoming": 3,
            "in_progress": 1,
            "failed": 0,
            "waiting_for_input": 2,
            "done": 5,
            "archived": 7,
        }
    )

    assert _tabs(keyboard) == {
        "incoming": "• 📥 Новые 3 •",
        "in_progress": "⚙️ Работа 1",
        "failed": "❌ Ошибки 0",
        "waiting_for_input": "⚠️ Вопросы 2",
        "done": "✅ Готово 5",
        "archived": "📦 Архив",
    }


def test_category_counts_default_missing_statuses_to_zero() -> None:
    keyboard = task_categories_keyboard(active_category="done", counts={})

    assert _tabs(keyboard) == {
        "incoming": "📥 Новые 0",
        "in_progress": "⚙️ Работа 0",
        "failed": "❌ Ошибки 0",
        "waiting_for_input": "⚠️ Вопросы 0",
        "done": "• ✅ Готово 0 •",
        "archived": "📦 Архив",
    }


def test_done_counter_is_visible_for_zero_and_nonzero_counts() -> None:
    zero_tabs = _tabs(task_categories_keyboard(counts={"done": 0}))
    nonzero_tabs = _tabs(task_categories_keyboard(counts={"done": 42}))

    assert zero_tabs["done"] == "✅ Готово 0"
    assert nonzero_tabs["done"] == "✅ Готово 42"


def test_compact_labels_do_not_return_long_category_titles() -> None:
    labels = _tabs(
        task_categories_keyboard(
            active_category="done",
            counts={
                "incoming": 1,
                "in_progress": 2,
                "failed": 3,
                "waiting_for_input": 4,
                "done": 5,
                "archived": 6,
            },
        )
    )

    for long_title in ("Входящие", "В работе", "Уточнение", "Завершённые"):
        assert all(long_title not in label for label in labels.values())


def test_archive_count_is_never_rendered_even_when_active() -> None:
    keyboard = task_categories_keyboard(
        active_category="archived",
        counts={"archived": 42, "incoming": 2},
    )

    assert _tabs(keyboard)["archived"] == "• 📦 Архив •"
    assert _tabs(keyboard)["incoming"] == "📥 Новые 2"


def test_callback_data_is_not_changed_by_counts() -> None:
    keyboard = task_categories_keyboard(active_category="failed", counts={"failed": 4})

    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("cat:")
    }
    assert callbacks == {
        "cat:incoming:1",
        "cat:in_progress:1",
        "cat:failed:1",
        "cat:waiting_for_input:1",
        "cat:done:1",
        "cat:archived:1",
    }


def test_without_counts_labels_keep_legacy_plain_text() -> None:
    keyboard = task_categories_keyboard(active_category="incoming")

    assert _tabs(keyboard)["incoming"] == "• 📥 Новые •"
    assert _tabs(keyboard)["failed"] == "❌ Ошибки"
    assert _tabs(keyboard)["archived"] == "📦 Архив"
