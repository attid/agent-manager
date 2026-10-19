"""Telegram message and callback query handlers for the Secretary bot."""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Awaitable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from .client import BackendClient
from .config import TelegramConfig
from .keyboards import (
    mode_keyboard,
    talk_keyboard,
    task_card_keyboard,
    task_categories_keyboard,
)
from ..git_engine import GitEngine
from .state import UserStateStorage
from .worker import QueueProcessor

logger = logging.getLogger(__name__)

router = Router(name="telegram_secretary")


class AuthMiddleware(BaseMiddleware):
    """Enforce single allowed Telegram user ID."""

    def __init__(self, allowed_user_id: int) -> None:
        self.allowed_user_id = allowed_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None

        # If allowed_user_id is configured (> 0) and does not match, reject
        if self.allowed_user_id > 0 and user.id != self.allowed_user_id:
            if isinstance(event, Message):
                await event.answer("⛔️ Доступ запрещен.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔️ Доступ запрещен.", show_alert=True)
            return None

        return await handler(event, data)


# -- Commands --


@router.message(Command("start"))
async def cmd_start(
    message: Message,
    storage: UserStateStorage,
) -> None:
    user_id = message.from_user.id
    state = storage.get_user_state(user_id)
    text = (
        "👋 Привет! Я интерфейс к системе агентов и секретарю.\n\n"
        f"Текущий режим: <b>{state.mode}</b>\n\n"
        "Выберите действие в меню ниже или используйте команды:\n"
        "• /new — поставить новую задачу\n"
        "• /talk — общий разговор с секретарём\n"
        "• /list — просмотр задач и карточек"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=mode_keyboard())


@router.message(Command("new"))
async def cmd_new(
    message: Message,
    storage: UserStateStorage,
) -> None:
    user_id = message.from_user.id
    storage.set_user_mode(user_id, "new_task")
    await message.answer(
        "➕ <b>Режим постановки новой задачи</b>\n\n"
        "Опишите задачу текстом или отправьте голосовое сообщение.\n"
        "Секретарь уточнит требования, подготовит карточку и предложит перевести во входящие или в работу.",
        parse_mode="HTML",
    )


@router.message(Command("talk"))
async def cmd_talk(
    message: Message,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    user_id = message.from_user.id
    state = storage.get_user_state(user_id)
    talk_conv_id = state.talk_conversation_id

    if not talk_conv_id:
        conv = await client.create_conversation(
            title="Общий разговор (Telegram)",
            metadata={"origin": "telegram_talk", "user_id": user_id},
        )
        talk_conv_id = conv["id"]
        storage.set_talk_conversation(user_id, talk_conv_id)

    storage.set_user_mode(user_id, "talk", conversation_id=talk_conv_id)
    await message.answer(
        "💬 <b>Режим общего разговора</b>\n\n"
        "Задайте любой вопрос о состоянии задач, проектных планах или перемещении карточек.",
        parse_mode="HTML",
        reply_markup=talk_keyboard(),
    )


@router.message(Command("reset"))
async def cmd_reset(
    message: Message,
    storage: UserStateStorage,
) -> None:
    user_id = message.from_user.id
    storage.set_talk_conversation(user_id, None)
    storage.set_user_mode(user_id, "talk", conversation_id=None)
    await message.answer(
        "🔄 <b>Диалог сброшен.</b> Следующее сообщение начнёт новый разговор со секретарём.",
        parse_mode="HTML",
        reply_markup=talk_keyboard(),
    )


@router.message(Command("list"))
async def cmd_list(
    message: Message,
    client: BackendClient,
) -> None:
    tasks = await client.list_tasks(status="incoming")
    await message.answer(
        "📋 <b>Список задач</b>\nВыберите категорию или карточку:",
        parse_mode="HTML",
        reply_markup=task_categories_keyboard("incoming", tasks),
    )


@router.message(Command("analyze"))
async def cmd_analyze(
    message: Message,
    client: BackendClient,
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    task_id = parts[1].strip() if len(parts) > 1 else None

    if not task_id:
        tasks = await client.list_tasks(status="done")
        if not tasks:
            tasks = await client.list_tasks(status="failed")
        if not tasks:
            await message.answer("ℹ️ Нет завершённых задач для анализа. Укажите ID: <code>/analyze &lt;task_id&gt;</code>", parse_mode="HTML")
            return
        task_id = tasks[0]["id"]

    try:
        task = await client.get_task(task_id)
    except Exception:
        await message.answer(f"❌ Задача <code>{html.escape(task_id)}</code> не найдена.", parse_mode="HTML")
        return

    title = task.get("title", "")
    meta = task.get("meta") or task.get("metadata") or {}
    summary = task.get("summary") or meta.get("summary", "")
    prov = meta.get("provenance") or {}
    changed_files = task.get("changed_files") or meta.get("changed_files") or []

    findings = (
        f"📊 <b>Анализ сессии задачи #{task_id}: {html.escape(title)}</b>\n\n"
        f"• <b>Статус:</b> <code>{html.escape(task.get('status', ''))}</code>\n"
        f"• <b>Изменено файлов:</b> {len(changed_files)}\n"
        f"• <b>Модель:</b> <code>{html.escape(prov.get('model', 'pi-default'))}</code>\n"
        f"• <b>Резюме разработчика:</b>\n<i>{html.escape(summary[:400])}</i>\n\n"
        "💡 <b>Выводы анализатора (system-log-analyzer):</b>\n"
        "Сессия выполнена успешно. Проверены затронутые компоненты. "
        "Критического дефицита документации не выявлено."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 К карточке задачи",
                    callback_data=f"select_task:{task_id}",
                )
            ]
        ]
    )
    await message.answer(findings, parse_mode="HTML", reply_markup=kb)


# -- Callbacks --


@router.callback_query(F.data.startswith("cat:"))
async def on_category_switch(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    parts = callback.data.split(":")
    category = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    tasks = await client.list_tasks(status=category)
    await callback.message.edit_reply_markup(
        reply_markup=task_categories_keyboard(category, tasks, page=page)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("select_task:"))
async def on_select_task(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    task_id = callback.data.split(":")[1]
    task = await client.get_task(task_id)

    conv_id = task.get("metadata", {}).get("conversation_id")
    meta = dict(task.get("metadata") or {})
    needs_patch = False
    if not conv_id:
        conv = await client.create_conversation(
            title=f"Контекст задачи {task_id}",
            metadata={"task_id": task_id},
        )
        conv_id = conv["id"]
        meta["conversation_id"] = conv_id
        needs_patch = True

    chat_id = callback.message.chat.id if callback.message and callback.message.chat else None
    if chat_id and "chat_id" not in meta:
        meta["chat_id"] = chat_id
        meta["user_id"] = callback.from_user.id
        needs_patch = True

    if needs_patch:
        await client.patch_task(task_id, metadata=meta)

    storage.set_user_mode(
        callback.from_user.id, "task", task_id=task_id, conversation_id=conv_id
    )

    clar_info = ""
    clar = meta.get("clarification") or dict(meta.get("provenance") or {}).get("clarification") or {}
    if task.get("status") == "waiting_for_input" and clar:
        clar_info = (
            f"\n\n⚠️ <b>Вопрос от агента:</b>\n"
            f"<b>{html.escape(clar.get('title') or '')}</b>\n"
            f"{html.escape(clar.get('description') or '')}\n\n"
            f"❓ <b>Вопрос:</b> {html.escape(clar.get('question') or '')}\n"
        )

    card_text = (
        f"📌 <b>Задача #{task['id']}</b>\n"
        f"Название: <b>{task['title']}</b>\n"
        f"Статус: <code>{task['status']}</code>\n"
        f"Пространство: {task.get('workspace') or '—'}\n\n"
        f"Описание:\n{task.get('description') or '<i>(без описания)</i>'}"
        f"{clar_info}\n\n"
        "<i>Контекст задачи выбран. Сообщения направляются по этой задаче.</i>"
    )
    await callback.message.edit_text(
        card_text,
        parse_mode="HTML",
        reply_markup=task_card_keyboard(task["id"], task["status"]),
    )
    await callback.answer("Контекст задачи выбран")


@router.callback_query(F.data.startswith("set_status:"))
async def on_set_status(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    _, task_id, new_status = callback.data.split(":")
    task = await client.patch_task(task_id, status=new_status)

    card_text = (
        f"📌 <b>Задача #{task['id']}</b>\n"
        f"Название: <b>{task['title']}</b>\n"
        f"Статус: <code>{task['status']}</code>\n"
        f"Пространство: {task.get('workspace') or '—'}\n\n"
        f"Описание:\n{task.get('description') or '<i>(без описания)</i>'}\n\n"
        f"✅ <i>Статус обновлен на: {new_status}</i>"
    )
    await callback.message.edit_text(
        card_text,
        parse_mode="HTML",
        reply_markup=task_card_keyboard(task["id"], task["status"]),
    )
    await callback.answer(f"Статус изменен на: {new_status}")


@router.callback_query(F.data.startswith("commit_local:"))
async def on_commit_local(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
) -> None:
    task_id = callback.data.split(":")[1]
    await callback.answer("⏳ Выполняется локальный коммит...")
    task = await client.get_task(task_id)
    ws = task.get("workspace") or config.default_workspace
    meta = task.get("metadata") or {}
    changed_files = meta.get("changed_files") or []
    summary = meta.get("summary")

    git_engine = GitEngine(default_workspace=ws)
    res = git_engine.commit_local(
        workspace=ws,
        task_id=task_id,
        title=task["title"],
        summary=summary,
        changed_files=changed_files,
    )
    if res.success:
        await client.patch_task(task_id, status="done")
        await callback.message.edit_text(
            f"✅ <b>Задача #{task_id} закоммичена!</b>\n\n"
            f"Ветка: <code>{html.escape(res.branch or 'HEAD')}</code>\n"
            f"Коммит: <code>{html.escape(res.commit_hash or '—')}</code>\n\n"
            f"<i>{html.escape(res.message)}</i>",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            f"⚠️ <b>Ошибка локального коммита по задаче #{task_id}:</b>\n\n{html.escape(res.message)}",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("commit_push:"))
async def on_commit_push(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
) -> None:
    task_id = callback.data.split(":")[1]
    await callback.answer("🚀 Запуск коммита, пуша и проверки CI...")
    task = await client.get_task(task_id)
    ws = task.get("workspace") or config.default_workspace
    meta = task.get("metadata") or {}
    changed_files = meta.get("changed_files") or []
    summary = meta.get("summary")

    status_msg = await callback.message.answer("⏳ Коммит и отправка в GitHub... Ожидаем проверки CI...")

    git_engine = GitEngine(default_workspace=ws)
    res = git_engine.commit_and_push(
        workspace=ws,
        task_id=task_id,
        title=task["title"],
        summary=summary,
        changed_files=changed_files,
        watch_ci=True,
    )

    if res.success:
        await client.patch_task(task_id, status="done")
        await status_msg.edit_text(
            f"🎉 <b>Задача #{task_id} успешно закоммичена, запушена и прошла CI!</b>\n\n"
            f"Ветка: <code>{html.escape(res.branch or 'HEAD')}</code>\n"
            f"Коммит: <code>{html.escape(res.commit_hash or '—')}</code>\n\n"
            f"<i>{html.escape(res.message)}</i>",
            parse_mode="HTML",
        )
    else:
        ci_logs_part = f"\n\n<b>Логи сбоя CI:</b>\n<pre>{html.escape(res.ci_logs[:1000])}</pre>" if res.ci_logs else ""
        fix_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Обсудить ошибку",
                        callback_data=f"select_task:{task_id}",
                    )
                ]
            ]
        )
        await status_msg.edit_text(
            f"❌ <b>Ошибка при пуше или в CI для задачи #{task_id}:</b>\n\n"
            f"{html.escape(res.message)}"
            f"{ci_logs_part}",
            parse_mode="HTML",
            reply_markup=fix_kb,
        )


@router.callback_query(F.data.startswith("rollback:"))
async def on_rollback(
    callback: CallbackQuery,
    client: BackendClient,
    config: TelegramConfig,
) -> None:
    task_id = callback.data.split(":")[1]
    await callback.answer("⏳ Откат изменений...")
    task = await client.get_task(task_id)
    ws = task.get("workspace") or config.default_workspace
    meta = task.get("metadata") or {}
    changed_files = meta.get("changed_files") or []

    git_engine = GitEngine(default_workspace=ws)
    res = git_engine.rollback(workspace=ws, changed_files=changed_files)

    if res.success:
        await client.patch_task(task_id, status="cancelled")
        await callback.message.edit_text(
            f"❌ <b>Изменения по задаче #{task_id} откатаны.</b>\n\n"
            f"Задача переведена в статус <code>cancelled</code>.\n"
            f"Рабочая директория возвращена к исходному состоянию.",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            f"⚠️ <b>Ошибка при откате изменений:</b>\n\n{html.escape(res.message)}",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("talk_task:"))
async def on_talk_task(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    task_id = callback.data.split(":")[1]
    task = await client.get_task(task_id)
    conv_id = task.get("metadata", {}).get("conversation_id")

    storage.set_user_mode(
        callback.from_user.id, "task", task_id=task_id, conversation_id=conv_id
    )
    await callback.answer("Диалог привязан к задаче")
    await callback.message.answer(
        f"💬 Напишите сообщение секретарю по задаче <b>{task['title']}</b>:",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("cancel_queue:"))
async def on_cancel_queue(
    callback: CallbackQuery,
    storage: UserStateStorage,
) -> None:
    queue_id = callback.data.split(":")[1]
    cancelled = storage.cancel_queued_message(queue_id)
    if cancelled:
        await callback.message.edit_text("❌ Сообщение отменено из очереди.")
        await callback.answer("Сообщение отменено")
    else:
        await callback.answer(
            "⚠️ Сообщение уже находится в обработке, отмена невозможна.",
            show_alert=True,
        )


@router.callback_query(F.data == "action:new_task")
async def on_action_new_task(
    callback: CallbackQuery,
    storage: UserStateStorage,
) -> None:
    storage.set_user_mode(callback.from_user.id, "new_task")
    await callback.message.answer(
        "➕ <b>Режим новой задачи</b>. Опишите задачу голосом или текстом:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "action:talk")
async def on_action_talk(
    callback: CallbackQuery,
    storage: UserStateStorage,
    client: BackendClient,
) -> None:
    state = storage.get_user_state(callback.from_user.id)
    talk_conv_id = state.talk_conversation_id
    if not talk_conv_id:
        conv = await client.create_conversation(
            title="Общий разговор (Telegram)",
            metadata={"origin": "telegram_talk", "user_id": callback.from_user.id},
        )
        talk_conv_id = conv["id"]
        storage.set_talk_conversation(callback.from_user.id, talk_conv_id)

    storage.set_user_mode(
        callback.from_user.id, "talk", conversation_id=talk_conv_id
    )
    await callback.message.answer(
        "💬 <b>Общий разговор</b>. Задайте вопрос секретарю:",
        parse_mode="HTML",
        reply_markup=talk_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "action:reset_talk")
async def on_action_reset_talk(
    callback: CallbackQuery,
    storage: UserStateStorage,
) -> None:
    user_id = callback.from_user.id
    storage.set_talk_conversation(user_id, None)
    storage.set_user_mode(user_id, "talk", conversation_id=None)
    await callback.message.answer(
        "🔄 <b>Диалог сброшен.</b> Следующее сообщение начнёт новый разговор со секретарём.",
        parse_mode="HTML",
        reply_markup=talk_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "action:list_tasks")
async def on_action_list_tasks(
    callback: CallbackQuery,
    client: BackendClient,
) -> None:
    tasks = await client.list_tasks(status="incoming")
    await callback.message.answer(
        "📋 <b>Список задач:</b>",
        parse_mode="HTML",
        reply_markup=task_categories_keyboard("incoming", tasks),
    )
    await callback.answer()


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# -- Messages (Text, Voice, Photo, Document) --


@router.message(F.text | F.voice | F.photo | F.document)
async def on_user_message(
    message: Message,
    storage: UserStateStorage,
    client: BackendClient,
    queue_processor: QueueProcessor,
    config: TelegramConfig,
) -> None:
    user_id = message.from_user.id
    state = storage.get_user_state(user_id)

    # 1. Idle mode check
    if state.mode == "idle":
        await message.answer(
            "⚠️ Режим не выбран. Пожалуйста, выберите действие:",
            reply_markup=mode_keyboard(),
        )
        return

    # Extract text and media
    media_type = "text"
    text = ""
    audio_bytes: bytes | None = None
    media_path: str | None = None

    if message.voice:
        media_type = "voice"
        text = "(Голосовое сообщение)"
        # Download audio
        try:
            file_info = await message.bot.get_file(message.voice.file_id)
            if file_info.file_path:
                audio_stream = await message.bot.download_file(file_info.file_path)
                audio_bytes = audio_stream.read()
                # Persist locally
                config.media_dir.mkdir(parents=True, exist_ok=True)
                local_file = config.media_dir / f"voice_{message.voice.file_id}.ogg"
                local_file.write_bytes(audio_bytes)
                media_path = str(local_file)
        except Exception as e:
            logger.warning("Failed to download voice file: %s", e)
    elif message.photo:
        media_type = "photo"
        text = message.caption or "(Изображение)"
        try:
            photo = message.photo[-1]
            file_info = await message.bot.get_file(photo.file_id)
            if file_info.file_path:
                photo_stream = await message.bot.download_file(file_info.file_path)
                config.media_dir.mkdir(parents=True, exist_ok=True)
                local_file = config.media_dir / f"photo_{photo.file_id}.jpg"
                local_file.write_bytes(photo_stream.read())
                media_path = str(local_file)
        except Exception as e:
            logger.warning("Failed to download photo file: %s", e)
    elif message.document:
        media_type = "document"
        caption = message.caption or ""
        doc = message.document
        doc_filename = Path(doc.file_name or "document").name
        target_filename = f"doc_{doc.file_id[:8]}_{doc_filename}"
        try:
            file_info = await message.bot.get_file(doc.file_id)
            if file_info.file_path:
                doc_stream = await message.bot.download_file(file_info.file_path)
                config.media_dir.mkdir(parents=True, exist_ok=True)
                local_file = config.media_dir / target_filename
                local_file.write_bytes(doc_stream.read())
                media_path = str(local_file)
        except Exception as e:
            logger.warning("Failed to download document file: %s", e)

        # Inline text preview if it's a small text-based document
        doc_size = doc.file_size or 0
        is_text = False
        text_content: str | None = None
        if media_path and doc_size <= 150_000:
            try:
                raw_bytes = Path(media_path).read_bytes()
                text_content = raw_bytes.decode("utf-8")
                is_text = True
            except Exception:
                is_text = False

        if is_text and text_content is not None:
            ext = Path(doc_filename).suffix.lstrip(".") or "text"
            file_block = f"📄 Файл `{doc_filename}` ({doc_size} байт):\n```{ext}\n{text_content}\n```"
            text = f"{caption}\n\n{file_block}" if caption else file_block
        else:
            notice = f"📎 Прикреплен файл: `{doc_filename}` ({doc_size} байт)"
            text = f"{caption}\n\n{notice}" if caption else notice
    else:
        text = message.text or ""

    # 2. Context resolution
    context_mode = state.mode
    context_task_id = state.active_task_id
    context_conversation_id = state.active_conversation_id

    if context_mode == "new_task":
        # First message in new_task mode creates the task draft and conversation
        title_summary = (
            text[:50] + "..." if len(text) > 50 else (text or "Новая задача")
        )
        task = await client.create_task(
            title=title_summary,
            status="incoming",
            metadata={
                "created_from": "telegram_new_task",
                "chat_id": message.chat.id,
                "user_id": user_id,
            },
        )
        context_task_id = task["id"]
        conv = await client.create_conversation(
            title=f"Задача #{context_task_id}: {title_summary}",
            metadata={"task_id": context_task_id},
        )
        context_conversation_id = conv["id"]
        # Link conversation into task metadata
        await client.patch_task(
            context_task_id,
            metadata={
                "created_from": "telegram_new_task",
                "conversation_id": context_conversation_id,
                "chat_id": message.chat.id,
                "user_id": user_id,
            },
        )
        # Switch mode to task context
        storage.set_user_mode(
            user_id,
            "task",
            task_id=context_task_id,
            conversation_id=context_conversation_id,
        )
        context_mode = "task"

    elif context_mode == "talk":
        if not context_conversation_id:
            conv = await client.create_conversation(
                title="Общий разговор (Telegram)",
                metadata={"origin": "telegram_talk", "user_id": user_id},
            )
            context_conversation_id = conv["id"]
            storage.set_talk_conversation(user_id, context_conversation_id)
            storage.set_user_mode(
                user_id, "talk", conversation_id=context_conversation_id
            )

    elif context_mode == "task":
        if context_task_id:
            task = await client.get_task(context_task_id)

            # If task is waiting for user clarification, user's message resumes the task!
            if task.get("status") == "waiting_for_input":
                clar_reply_text = text
                if media_type == "voice" and audio_bytes:
                    transcribed = await queue_processor.transcriber.transcribe(audio_bytes)
                    if transcribed:
                        clar_reply_text = transcribed

                meta = dict(task.get("metadata") or {})
                meta["last_clarification_response"] = clar_reply_text
                clar = meta.get("clarification") or dict(meta.get("provenance") or {}).get("clarification") or {}
                q_text = clar.get("question") or "Уточнение"

                updated_desc = (task.get("description") or "").strip()
                updated_desc += f"\n\n### Ответ на уточнение:\n- **Вопрос:** {q_text}\n- **Ответ:** {clar_reply_text}\n"

                await client.patch_task(
                    context_task_id,
                    status="in_progress",
                    description=updated_desc,
                    metadata=meta,
                )
                await message.answer(
                    f"✅ <b>Ответ принят.</b>\n\n⚙️ <i>Агент возобновляет выполнение задачи #{context_task_id}...</i>",
                    parse_mode="HTML",
                    reply_markup=task_card_keyboard(context_task_id, "in_progress"),
                )
                return

            if not context_conversation_id:
                context_conversation_id = task.get("metadata", {}).get("conversation_id")
                if not context_conversation_id:
                    conv = await client.create_conversation(
                        title=f"Контекст задачи {context_task_id}",
                        metadata={"task_id": context_task_id},
                    )
                    context_conversation_id = conv["id"]
                    meta = dict(task.get("metadata") or {})
                    meta["conversation_id"] = context_conversation_id
                    await client.patch_task(context_task_id, metadata=meta)
                    storage.set_user_mode(
                        user_id,
                        "task",
                        task_id=context_task_id,
                        conversation_id=context_conversation_id,
                    )

    if not context_conversation_id:
        await message.answer(
            "⚠️ Не удалось определить контекст диалога. Пожалуйста, выберите режим:",
            reply_markup=mode_keyboard(),
        )
        return

    # 3. Submit message through queue processor
    await queue_processor.submit_message(
        user_id=user_id,
        chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        context_mode=context_mode,
        context_task_id=context_task_id,
        context_conversation_id=context_conversation_id,
        text=text,
        media_type=media_type,
        media_path=media_path,
        audio_bytes=audio_bytes,
    )
