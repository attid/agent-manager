"""Asynchronous message processor for the Telegram bot queue."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile, InputRichMessage

from agent_manager.knowledge import KnowledgeBase, SEND_FILE_PATTERN
from .client import BackendClient
from .keyboards import cancel_queue_keyboard, talk_keyboard
from .state import QueuedMessage, UserStateStorage
from .transcription import DeepgramTranscriber

logger = logging.getLogger(__name__)


class QueueProcessor:
    """Processes messages sequentially per conversation."""

    def __init__(
        self,
        bot: Bot,
        backend_client: BackendClient,
        storage: UserStateStorage,
        transcriber: DeepgramTranscriber,
        *,
        knowledge_base: KnowledgeBase | None = None,
        poll_interval_seconds: float = 0.5,
        turn_timeout_seconds: float = 120.0,
    ) -> None:
        self.bot = bot
        self.client = backend_client
        self.storage = storage
        self.transcriber = transcriber
        self.knowledge_base = knowledge_base
        self.poll_interval_seconds = poll_interval_seconds
        self.turn_timeout_seconds = turn_timeout_seconds

    async def submit_message(
        self,
        *,
        user_id: int,
        chat_id: int,
        telegram_message_id: int,
        context_mode: str,
        context_task_id: str | None,
        context_conversation_id: str,
        text: str,
        media_type: str = "text",
        media_path: str | None = None,
        audio_bytes: bytes | None = None,
    ) -> QueuedMessage:
        """Enqueue message and either dispatch immediately or show queue indicator."""
        is_busy = self.storage.is_conversation_busy(context_conversation_id)

        if is_busy:
            # Enqueue as pending with cancel button
            queued_msg = self.storage.enqueue_message(
                user_id=user_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                context_mode=context_mode,
                context_task_id=context_task_id,
                context_conversation_id=context_conversation_id,
                text=text,
                media_type=media_type,
                media_path=media_path,
            )
            try:
                status_msg = await self.bot.send_message(
                    chat_id=chat_id,
                    text="⏳ Сообщение в очереди на обработку.",
                    reply_markup=cancel_queue_keyboard(queued_msg.id),
                    reply_to_message_id=telegram_message_id,
                )
                self.storage.update_status_message_id(
                    queued_msg.id, status_msg.message_id
                )
            except Exception as e:
                logger.warning("Failed to send queue status message: %s", e)
            return queued_msg

        # Immediately claim and process
        queued_msg = self.storage.enqueue_message(
            user_id=user_id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            context_mode=context_mode,
            context_task_id=context_task_id,
            context_conversation_id=context_conversation_id,
            text=text,
            media_type=media_type,
            media_path=media_path,
        )
        self.storage.set_message_status(queued_msg.id, "processing")

        initial_status_text = (
            "🎙 Расшифровываю аудио..."
            if media_type == "voice"
            else "⏳ Жду ответ секретаря..."
        )
        try:
            status_msg = await self.bot.send_message(
                chat_id=chat_id,
                text=initial_status_text,
                reply_to_message_id=telegram_message_id,
            )
            self.storage.update_status_message_id(
                queued_msg.id, status_msg.message_id
            )
        except Exception as e:
            logger.warning("Failed to send initial status message: %s", e)
            status_msg = None

        # Run processing in background task
        asyncio.create_task(
            self.process_message(
                queued_msg,
                audio_bytes=audio_bytes,
                status_message_id=(
                    status_msg.message_id if status_msg else None
                ),
            )
        )
        return queued_msg

    async def process_message(
        self,
        queued_msg: QueuedMessage,
        audio_bytes: bytes | None = None,
        status_message_id: int | None = None,
    ) -> None:
        """Process one claimed message against backend secretary turn."""
        status_msg_id = status_message_id or queued_msg.status_message_id
        chat_id = queued_msg.chat_id
        conv_id = queued_msg.context_conversation_id
        text = queued_msg.text

        try:
            # 1. Transcribe voice if needed
            if queued_msg.media_type == "voice":
                if status_msg_id:
                    await self._safe_edit_text(
                        chat_id, status_msg_id, "🎙 Расшифровываю аудио..."
                    )
                if audio_bytes:
                    transcribed = await self.transcriber.transcribe(audio_bytes)
                    if transcribed:
                        text = f"[Голосовое сообщение (автоматическая расшифровка речи, возможны неточности распознавания)]:\n{transcribed}"
                if status_msg_id:
                    await self._safe_edit_text(
                        chat_id, status_msg_id, "⏳ Жду ответ секретаря..."
                    )

            # 2. Append user message to backend. Inbound media stays in the
            # explicit Telegram media directory; the persistent conversation
            # handler is the single owner that stages it into the
            # conversation/turn-scoped attachment store.
            append_res = await self.client.append_message(
                conv_id,
                text=text,
                metadata={
                    "telegram_message_id": queued_msg.telegram_message_id,
                    "media_type": queued_msg.media_type,
                    "media_path": queued_msg.media_path,
                },
            )
            input_message_id = append_res["id"]

            # 3. Enqueue turn on backend
            turn = await self.client.create_turn(conv_id, input_message_id)
            turn_id = turn["id"]

            # 4. Wait for turn to finish (polling dispatcher outcome)
            elapsed = 0.0
            reply_text: str | None = None
            turn_error: str | None = None

            while elapsed < self.turn_timeout_seconds:
                await asyncio.sleep(self.poll_interval_seconds)
                elapsed += self.poll_interval_seconds
                current_turn = await self.client.get_turn(turn_id)
                turn_status = current_turn.get("status")

                if turn_status == "completed":
                    reply_id = current_turn.get("reply_message_id")
                    if reply_id:
                        messages = await self.client.list_messages(conv_id)
                        for m in messages:
                            if m["id"] == reply_id:
                                reply_text = m.get("text")
                                break
                    if not reply_text:
                        reply_text = "Ответ получен, но текст пуст."
                    break

                if turn_status == "failed":
                    turn_error = current_turn.get(
                        "error", "Неизвестная ошибка выполнения хода"
                    )
                    break

            if reply_text is not None:
                self.storage.set_message_status(queued_msg.id, "completed")

                # Extract and dispatch file attachments (e.g. [SEND_FILE: path])
                file_targets = list(SEND_FILE_PATTERN.findall(reply_text))
                clean_reply_text = SEND_FILE_PATTERN.sub("", reply_text).strip()
                if not clean_reply_text and file_targets:
                    clean_reply_text = "📄 Запрошенный документ:"
                elif not clean_reply_text:
                    clean_reply_text = "Ответ получен."

                if status_msg_id:
                    edit_success = False
                    if len(clean_reply_text) <= 16384:
                        try:
                            await self.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=status_msg_id,
                                text=clean_reply_text,
                                rich_message=InputRichMessage(markdown=clean_reply_text),
                            )
                            edit_success = True
                        except Exception as edit_err:
                            logger.debug("edit_message_text with rich_message failed: %s", edit_err)
                            try:
                                await self.bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=status_msg_id,
                                    text=clean_reply_text,
                                )
                                edit_success = True
                            except Exception:
                                pass

                    if not edit_success:
                        doc_file = BufferedInputFile(
                            clean_reply_text.encode("utf-8"), filename="ответ_секретаря.md"
                        )
                        await self.bot.send_document(
                            chat_id=chat_id,
                            document=doc_file,
                            caption="📄 Текст ответа прикреплён файлом:",
                            reply_to_message_id=queued_msg.telegram_message_id,
                        )
                else:
                    if len(clean_reply_text) <= 16384:
                        try:
                            await self.bot.send_rich_message(
                                chat_id=chat_id,
                                rich_message=InputRichMessage(markdown=clean_reply_text),
                                reply_to_message_id=queued_msg.telegram_message_id,
                            )
                        except Exception as send_err:
                            logger.warning("send_rich_message failed, fallback to doc: %s", send_err)
                            doc_file = BufferedInputFile(
                                clean_reply_text.encode("utf-8"), filename="ответ_секретаря.md"
                            )
                            await self.bot.send_document(
                                chat_id=chat_id,
                                document=doc_file,
                                caption="📄 Текст ответа прикреплён файлом:",
                                reply_to_message_id=queued_msg.telegram_message_id,
                            )
                    else:
                        doc_file = BufferedInputFile(
                            clean_reply_text.encode("utf-8"), filename="ответ_секретаря.md"
                        )
                        await self.bot.send_document(
                            chat_id=chat_id,
                            document=doc_file,
                            caption="📄 Ответ секретаря превышает 16k символов. Текст прикреплён файлом:",
                            reply_to_message_id=queued_msg.telegram_message_id,
                        )

                # Send attached documents if found
                for file_query in file_targets:
                    resolved_file = (
                        self.knowledge_base.resolve_file(file_query)
                        if self.knowledge_base is not None
                        else None
                    )
                    if resolved_file and resolved_file.is_file():
                        try:
                            await self.bot.send_document(
                                chat_id=chat_id,
                                document=FSInputFile(resolved_file),
                                caption=f"📄 {resolved_file.name}",
                            )
                            logger.info(
                                "Sent document %s to chat_id=%s",
                                resolved_file,
                                chat_id,
                            )
                        except Exception as doc_err:
                            logger.error(
                                "Failed to send document %s: %s",
                                resolved_file,
                                doc_err,
                                exc_info=doc_err,
                            )
                    else:
                        logger.warning(
                            "Requested document could not be resolved: %s", file_query
                        )
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=f"⚠️ Документ не найден: <code>{file_query}</code>",
                            parse_mode="HTML",
                        )
            elif turn_error is not None:
                self.storage.set_message_status(queued_msg.id, "failed")
                is_talk = queued_msg.context_mode == "talk"
                if is_talk:
                    self.storage.set_talk_conversation(queued_msg.user_id, None)
                    state = self.storage.get_user_state(queued_msg.user_id)
                    if state.active_conversation_id == queued_msg.context_conversation_id:
                        self.storage.set_user_mode(queued_msg.user_id, "talk", conversation_id=None)
                err_msg = (
                    f"❌ <b>Ошибка секретаря:</b>\n{turn_error}\n\n"
                    "<i>Контекст диалога сброшен. Следующее сообщение начнёт новый разговор.</i>"
                    if is_talk
                    else f"❌ Ошибка секретаря:\n{turn_error}"
                )
                markup = talk_keyboard() if is_talk else None
                if status_msg_id:
                    await self._safe_edit_text(chat_id, status_msg_id, err_msg, reply_markup=markup)
                else:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=err_msg,
                        parse_mode="HTML",
                        reply_to_message_id=queued_msg.telegram_message_id,
                        reply_markup=markup,
                    )
            else:
                self.storage.set_message_status(queued_msg.id, "failed")
                is_talk = queued_msg.context_mode == "talk"
                if is_talk:
                    self.storage.set_talk_conversation(queued_msg.user_id, None)
                    state = self.storage.get_user_state(queued_msg.user_id)
                    if state.active_conversation_id == queued_msg.context_conversation_id:
                        self.storage.set_user_mode(queued_msg.user_id, "talk", conversation_id=None)
                timeout_msg = (
                    "⏱ <b>Время ожидания ответа секретаря истекло.</b>\n\n"
                    "<i>Контекст диалога сброшен. Следующее сообщение начнёт новый разговор.</i>"
                    if is_talk
                    else "⏱ Время ожидания ответа секретаря истекло."
                )
                markup = talk_keyboard() if is_talk else None
                if status_msg_id:
                    await self._safe_edit_text(
                        chat_id, status_msg_id, timeout_msg, reply_markup=markup
                    )
                else:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=timeout_msg,
                        parse_mode="HTML",
                        reply_to_message_id=queued_msg.telegram_message_id,
                        reply_markup=markup,
                    )

        except Exception as error:
            logger.exception("Error processing message %s: %s", queued_msg.id, error)
            self.storage.set_message_status(queued_msg.id, "failed")
            fail_text = f"❌ Не удалось обработать сообщение: {error}"
            if status_msg_id:
                await self._safe_edit_text(chat_id, status_msg_id, fail_text)
            else:
                try:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=fail_text,
                        reply_to_message_id=queued_msg.telegram_message_id,
                    )
                except Exception:
                    pass

        finally:
            # Drain next pending message in queue for this conversation
            next_msg = self.storage.claim_next_pending(conv_id)
            if next_msg is not None:
                if next_msg.status_message_id:
                    await self._safe_edit_text(
                        next_msg.chat_id,
                        next_msg.status_message_id,
                        "⏳ Обрабатываю запрос из очереди...",
                        reply_markup=None,
                    )
                asyncio.create_task(self.process_message(next_msg))

    async def _safe_edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any = None,
    ) -> None:
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.debug("safe_edit_text exception (harmless): %s", e)
