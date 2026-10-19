"""Telegram notification sender for task execution outcomes."""

from __future__ import annotations

import asyncio
import html
import logging
import math
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from agent_manager.storage import Task
from agent_manager.task_worker import TaskOutcome

from .keyboards import task_display_number

logger = logging.getLogger(__name__)


def format_duration_ms(duration_ms: float) -> str:
    """Format a millisecond duration using compact Russian time units.

    The value is rounded to whole seconds before decomposition, so values just
    below a unit boundary do not produce e.g. ``60 сек`` or ``60 мин``.
    Zero components are omitted; a zero duration is rendered as ``0 сек``.
    """
    total_seconds = max(0, math.floor(duration_ms / 1000.0 + 0.5))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if seconds or not parts:
        parts.append(f"{seconds} сек")
    return " ".join(parts)


def build_task_outcome_keyboard(
    task_id: str, status: str = "completed"
) -> InlineKeyboardMarkup:
    """Build inline keyboard for completed/failed/waiting/ready_to_commit task notification."""
    if status == "ready_to_commit":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Закоммитить",
                        callback_data=f"commit_local:{task_id}",
                    ),
                    InlineKeyboardButton(
                        text="🚀 Закоммитить и запушить",
                        callback_data=f"commit_push:{task_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Откатить",
                        callback_data=f"rollback:{task_id}",
                    ),
                    InlineKeyboardButton(
                        text="💬 Обсудить",
                        callback_data=f"select_task:{task_id}",
                    ),
                ],
            ]
        )

    action_text = (
        "✍️ Ответить агенту"
        if status == "waiting_for_input"
        else "💬 Обсудить результат"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=action_text,
                    callback_data=f"select_task:{task_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 Список задач",
                    callback_data="action:list_tasks",
                ),
            ],
        ]
    )


def format_task_outcome_message(task: Task, outcome: TaskOutcome) -> str:
    """Format an informative, safe HTML message for a task outcome."""
    max_summary_length = 2000
    max_error_length = 1500

    title_safe = html.escape(task.title)
    duration_str = format_duration_ms(outcome.duration_ms)

    if outcome.status == "waiting_for_input":
        meta = dict(getattr(task, "meta", None) or getattr(task, "metadata", None) or {})
        prov = dict(meta.get("provenance") or {})
        clar = prov.get("clarification") or meta.get("clarification") or {}
        clar_title = clar.get("title") or "Уточнение задачи"
        clar_desc = clar.get("description") or (outcome.summary or "")
        clar_q = clar.get("question") or "Как поступить?"

        return (
            f"⚠️ <b>Агенту требуется уточнение по задаче #{html.escape(task_display_number(task))}!</b>\n\n"
            f"📌 <b>{html.escape(clar_title)}</b>\n\n"
            f"{html.escape(clar_desc)}\n\n"
            f"❓ <b>Вопрос:</b> {html.escape(clar_q)}\n\n"
            f"<i>Ответьте на это сообщение текстом или голосом, чтобы агент продолжил выполнение.</i>"
        )
    elif outcome.status == "ready_to_commit":
        summary_raw = outcome.summary or "Изменения проверены и готовы к фиксации."
        if len(summary_raw) > max_summary_length:
            summary_raw = summary_raw[:max_summary_length] + "...\n<i>(текст обрезан)</i>"
        summary_safe = html.escape(summary_raw)

        files_part = ""
        if outcome.changed_files:
            file_lines = [f"• <code>{html.escape(f)}</code>" for f in outcome.changed_files[:15]]
            if len(outcome.changed_files) > 15:
                file_lines.append(f"<i>...и еще {len(outcome.changed_files) - 15} файлов</i>")
            files_part = "\n\n<b>Изменённые файлы:</b>\n" + "\n".join(file_lines)

        return (
            f"🔍 <b>Задача #{html.escape(task_display_number(task))} ожидает подтверждения!</b>\n"
            f"<b>Название:</b> {title_safe}\n"
            f"<b>Пространство:</b> {html.escape(task.workspace or 'default')}\n"
            f"⏱ <b>Время:</b> {duration_str}\n\n"
            f"<b>Результат:</b>\n{summary_safe}"
            f"{files_part}\n\n"
            f"<i>В рабочей директории есть изменения. Выберите действие ниже:</i>"
        )
    elif outcome.status == "completed":
        summary_raw = outcome.summary or "Задача успешно выполнена."
        if len(summary_raw) > max_summary_length:
            summary_raw = summary_raw[:max_summary_length] + "...\n<i>(текст обрезан)</i>"
        summary_safe = html.escape(summary_raw)

        files_part = ""
        if outcome.changed_files:
            file_lines = [f"• <code>{html.escape(f)}</code>" for f in outcome.changed_files[:15]]
            if len(outcome.changed_files) > 15:
                file_lines.append(f"<i>...и еще {len(outcome.changed_files) - 15} файлов</i>")
            files_part = "\n\n<b>Изменённые файлы:</b>\n" + "\n".join(file_lines)

        return (
            f"✅ <b>Задача #{html.escape(task_display_number(task))} завершена!</b>\n"
            f"<b>Название:</b> {title_safe}\n"
            f"<b>Пространство:</b> {html.escape(task.workspace or 'default')}\n"
            f"⏱ <b>Время:</b> {duration_str}\n\n"
            f"<b>Результат:</b>\n{summary_safe}"
            f"{files_part}"
        )
    else:
        error_raw = outcome.error or "Неизвестная ошибка выполнения."
        if len(error_raw) > max_error_length:
            error_raw = error_raw[:max_error_length] + "...\n<i>(текст обрезан)</i>"
        error_safe = html.escape(error_raw)

        return (
            f"❌ <b>Задача #{html.escape(task_display_number(task))} завершилась с ошибкой!</b>\n"
            f"<b>Название:</b> {title_safe}\n"
            f"<b>Пространство:</b> {html.escape(task.workspace or 'default')}\n"
            f"⏱ <b>Время:</b> {duration_str}\n\n"
            f"<b>Ошибка:</b>\n<pre>{error_safe}</pre>"
        )


class TelegramNotifier:
    """Thread-safe notifier that delivers task outcomes to Telegram."""

    def __init__(
        self,
        bot: Bot,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        default_chat_id: int | None = None,
    ) -> None:
        self.bot = bot
        self.loop = loop
        self.default_chat_id = default_chat_id

    def notify_sync(self, task: Task, outcome: TaskOutcome) -> None:
        """Synchronously enqueue or run task notification."""
        if self.loop is not None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.notify_async(task, outcome), self.loop)
        else:
            try:
                asyncio.run(self.notify_async(task, outcome))
            except Exception as exc:
                logger.error("Failed to run async notification: %s", exc, exc_info=exc)

    async def notify_async(self, task: Task | Any, outcome: TaskOutcome) -> None:
        """Deliver task outcome message to target Telegram chat."""
        meta = getattr(task, "meta", None)
        if not isinstance(meta, dict):
            raw_meta = getattr(task, "metadata", None)
            meta = raw_meta if isinstance(raw_meta, dict) else {}

        chat_id = meta.get("chat_id") or meta.get("user_id") or self.default_chat_id
        if not chat_id:
            logger.info(
                "No Telegram chat_id found for task %s, skipping notification",
                getattr(task, "id", None),
            )
            return

        text = format_task_outcome_message(task, outcome)
        keyboard = build_task_outcome_keyboard(task.id, outcome.status)

        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info(
                "Task outcome notification sent to chat_id=%s for task %s (status=%s)",
                chat_id,
                task.id,
                outcome.status,
            )
        except Exception as exc:
            logger.error(
                "Failed to send Telegram notification to chat_id=%s for task %s: %s",
                chat_id,
                task.id,
                exc,
                exc_info=exc,
            )
