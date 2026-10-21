"""Unified Telegram dialog delivery: rendering, sending and the outbox.

Every secretary-related message that reaches the owner's Telegram chat — a
copy of an API question, a secretary reply, a task-event result or an
execution error — is rendered here with the same rules: a kind header, the
full conversation UUID hashtag on every part and attachment caption, an
optional "other dialog" plate, and the explicit context-switch button.

Sending degrades gracefully: rich Markdown first, plain text second, a
document last — a formatting failure never loses the message or its context.

Delivery of durable outbox rows is at-least-once. A row is claimed
(``pending → sending``) before anything is sent, per-part progress is
persisted after every part, and a crash between claim and finish is recovered
by requeueing stale ``sending`` rows at startup. A part Telegram accepted
right before a crash may be delivered twice; that window is logged as an
"unknown outcome" rather than promised away.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Sequence

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InputRichMessage,
    ReplyParameters,
)
from sqlalchemy import select

from ..knowledge import SEND_FILE_PATTERN
from ..storage import Database
from ..storage.models import Conversation, Message, Task, TelegramDelivery
from ..storage.telegram_delivery import (
    DELIVERY_KIND_API_QUESTION,
    DELIVERY_KIND_REPLY,
    DELIVERY_KIND_TURN_ERROR,
    INPUT_SOURCE_API,
    MESSAGE_ORIGIN_BOT,
    TelegramDeliveryRepository,
    reply_delivery_key,
)
from .keyboards import dialog_switch_keyboard

logger = logging.getLogger(__name__)

#: Telegram's transport limit for one text message.
TELEGRAM_TEXT_LIMIT = 4096

#: Above this size the unified sender attaches the text as a document.
TELEGRAM_DOCUMENT_THRESHOLD = 16384

#: How a verified administrative principal is shown in question copies.
_ADMIN_CALLER_LABEL = "администратор"


def conversation_hashtag(conversation_id: str) -> str:
    """The full conversation UUID hashtag appended to dialog messages."""
    return f"#{conversation_id}"


def with_conversation_footer(text: str, conversation_id: str) -> str:
    """Append the conversation hashtag footer to one message part."""
    return f"{text.rstrip()}\n\n{conversation_hashtag(conversation_id)}"


def format_verified_caller(caller: str | None) -> str | None:
    """Render a trusted principal as a short human label, or ``None``.

    Only the server-injected verified-caller metadata is passed here; free-form
    client metadata is never treated as a confirmed identity.
    """
    if not caller:
        return None
    if caller == "admin":
        return _ADMIN_CALLER_LABEL
    if caller.startswith("execution:"):
        agent_id = caller[len("execution:"):].split("|", 1)[0]
        if agent_id:
            return f"агент: {agent_id}"
    return None


def api_question_header(caller: str | None = None) -> str:
    """Header for the Telegram copy of a question asked through the API."""
    header = "📨 Вопрос секретарю через API"
    agent = format_verified_caller(caller)
    return f"{header} ({agent})" if agent else header


def task_event_header(task_number: Any = None, task_title: Any = None) -> str:
    """Header for a background task result; never a user question copy."""
    title = f" «{task_title}»" if task_title else ""
    number = task_number if task_number is not None else "?"
    return f"📦 Результат задачи №{number}{title}"


def render_foreign_context_plate(
    *, active_task_number: Any = None, active_conversation_id: str | None = None
) -> str | None:
    """The 'this is another dialog' plate, or ``None`` when context matches.

    The plate is a snapshot taken when the message is sent; it is not a live
    indicator of the user's current context.
    """
    if active_task_number is not None:
        return f"↩️ Другой диалог · сейчас активна задача №{active_task_number}"
    if active_conversation_id is not None:
        return "↩️ Другой диалог"
    return None


def compose_dialog_message(
    *,
    header: str | None,
    body: str,
    plate: str | None = None,
) -> str:
    """Assemble header, plate and body — without the UUID footer.

    The footer is a transport concern: single-part sends append it once via
    :func:`with_conversation_footer`, multi-part sends append it to every part
    in :func:`split_for_telegram`. Composing it here would double it.
    """
    blocks = [block for block in (header, plate) if block]
    return "\n\n".join([*blocks, body.strip()]) if blocks else body.strip()


def split_for_telegram(
    text: str, conversation_id: str, *, limit: int = TELEGRAM_TEXT_LIMIT
) -> list[str]:
    """Split rendered text into parts, each carrying the UUID footer."""
    body = text.strip()
    footer_len = len(conversation_hashtag(conversation_id)) + 2
    chunk_limit = max(1, limit - footer_len)
    return [
        with_conversation_footer(
            body[start : start + chunk_limit], conversation_id
        )
        for start in range(0, len(body), chunk_limit)
    ]


def strip_file_directives(text: str) -> tuple[str, list[str]]:
    """Split SEND_FILE directives out of a reply text."""
    targets = list(SEND_FILE_PATTERN.findall(text))
    clean = SEND_FILE_PATTERN.sub("", text).strip()
    return clean, targets


async def send_markdown(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: int | None = None,
    keyboard: Any = None,
) -> int:
    """Send one text part: rich Markdown, then plain text, then a document.

    Returns the sent Telegram message id. Raises only when every transport
    failed — the caller decides whether the outcome is retryable.
    """
    reply_parameters = (
        ReplyParameters(message_id=reply_to_message_id)
        if reply_to_message_id is not None
        else None
    )
    try:
        message = await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(markdown=text),
            reply_parameters=reply_parameters,
            reply_markup=keyboard,
        )
        return message.message_id
    except Exception as rich_error:
        logger.debug("rich send failed, falling back to plain text: %s", rich_error)
    try:
        message = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_parameters=reply_parameters,
            reply_markup=keyboard,
        )
        return message.message_id
    except Exception as plain_error:
        logger.debug("plain send failed, falling back to document: %s", plain_error)
    message = await bot.send_document(
        chat_id=chat_id,
        document=BufferedInputFile(text.encode("utf-8"), filename="сообщение.md"),
        caption="📄 Текст прикреплён файлом:",
        reply_parameters=reply_parameters,
        reply_markup=keyboard,
    )
    return message.message_id


def find_task_for_conversation(database: Database, conversation_id: str) -> Task | None:
    """Return the most recently updated task bound to a conversation."""
    with database.session() as session:
        return (
            session.query(Task)
            .filter(Task.meta["conversation_id"].as_string() == conversation_id)
            .order_by(Task.updated_at.desc())
            .limit(1)
            .first()
        )


class TelegramDeliveryService:
    """Claims durable delivery rows and sends them through one code path."""

    def __init__(
        self,
        *,
        bot: Bot,
        database: Database,
        default_chat_id: int | None = None,
        knowledge_base: Any = None,
        active_context_probe: Callable[[], dict[str, Any] | None] | None = None,
        claim_grace_seconds: float = 2.0,
    ) -> None:
        self.bot = bot
        self.database = database
        self.default_chat_id = default_chat_id
        self.knowledge_base = knowledge_base
        self.active_context_probe = active_context_probe
        self.claim_grace_seconds = claim_grace_seconds
        self.repo = TelegramDeliveryRepository(database)

    # -- synchronous helper entry points ---------------------------------

    def startup_recovery(self) -> int:
        """Requeue rows left in ``sending`` by a previous process."""
        count = self.repo.requeue_stale_sending()
        if count:
            logger.info("telegram delivery startup recovery requeued %d rows", count)
        return count

    def claim_reply_delivery(self, turn_id: str) -> TelegramDelivery | None:
        """Claim the reply row for a turn the synchronous worker delivers."""
        row = self.repo.get_delivery_by_idempotency_key(reply_delivery_key(turn_id))
        if row is None:
            return None
        return row if self.repo.claim_delivery(row.id) else None

    def get_reply_delivery(self, turn_id: str) -> TelegramDelivery | None:
        """Return the reply row for a turn without claiming it."""
        return self.repo.get_delivery_by_idempotency_key(reply_delivery_key(turn_id))

    def record_delivery_sent(
        self,
        delivery: TelegramDelivery,
        *,
        chat_id: int,
        telegram_message_ids: Sequence[int],
    ) -> None:
        """Persist per-part progress and links after a synchronous send."""
        for index, telegram_message_id in enumerate(telegram_message_ids):
            self.repo.mark_part_sent(
                delivery.id,
                part_index=index,
                telegram_message_id=telegram_message_id,
            )
            self.repo.record_message_link(
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                conversation_id=delivery.conversation_id,
                message_id=delivery.message_id,
                turn_id=delivery.turn_id,
                task_id=(delivery.meta or {}).get("task_id"),
                origin=MESSAGE_ORIGIN_BOT,
            )
        self.repo.mark_sent(delivery.id)

    # -- outbox loop ------------------------------------------------------

    async def deliver_pending(self, *, limit: int = 5) -> int:
        """Deliver up to ``limit`` eligible pending rows.

        Stops the pass when a row is released back to pending: retrying the
        same head row inside one pass would just spin on it.
        """
        delivered = 0
        for _ in range(limit):
            row = self._claim_next()
            if row is None:
                break
            if await self.deliver_row(row) == "released":
                break
            delivered += 1
        return delivered

    async def deliver_row(self, row: TelegramDelivery) -> str:
        """Render and send one claimed delivery row.

        Returns the outcome: ``sent``, ``failed`` (terminal) or ``released``
        (back to pending — no recipient yet, or a send error whose outcome is
        unknown).
        """
        chat_id = self._resolve_chat_id(row)
        if chat_id is None:
            logger.info(
                "telegram delivery has no recipient yet; row stays pending",
                extra={
                    "event": "telegram_delivery_no_recipient",
                    "conversation_id": row.conversation_id,
                    "delivery_id": row.id,
                    "kind": row.kind,
                },
            )
            self.repo.release_claim(row.id)
            return "released"

        text = self._row_text(row)
        if text is None:
            self.repo.mark_failed(row.id, "message text is unavailable")
            return "failed"

        body, file_targets = strip_file_directives(text)
        if not body and file_targets:
            body = "📄 Запрошенный документ:"

        composed = compose_dialog_message(
            header=self._render_header(row),
            body=body,
            plate=self._foreign_plate(row),
        )

        task = find_task_for_conversation(self.database, row.conversation_id)
        keyboard = dialog_switch_keyboard(
            conversation_id=row.conversation_id,
            task_id=task.id if task is not None else None,
            task_number=task.number if task is not None else None,
        )

        parts: list[tuple[str, Any]]
        if len(composed) > TELEGRAM_DOCUMENT_THRESHOLD:
            parts = [("document", composed)]
        else:
            parts = [
                ("text", part)
                for part in split_for_telegram(composed, row.conversation_id)
            ]
        parts += [("file", target) for target in file_targets]

        try:
            await self._send_parts(row, chat_id, parts, keyboard)
        except Exception as error:
            return self._handle_send_error(row, error)

        self.repo.mark_sent(row.id)
        logger.info(
            "telegram delivery sent",
            extra={
                "event": "telegram_delivery_sent",
                "conversation_id": row.conversation_id,
                "message_id": row.message_id,
                "turn_id": row.turn_id,
                "chat_id": chat_id,
                "delivery_id": row.id,
                "kind": row.kind,
            },
        )
        return "sent"

    # -- internals ---------------------------------------------------------

    def _claim_next(self) -> TelegramDelivery | None:
        """Claim the next row, honouring the grace window for the worker.

        Fresh rows belong to the synchronous Telegram worker first: it answers
        the user inline within its polling window. Only rows older than the
        grace period — abandoned by a dead worker, or born while Telegram was
        offline — are picked up by this loop.
        """
        row = self.repo.claim_next_pending()
        if row is None:
            return None
        if self.claim_grace_seconds > 0:
            age = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - row.created_at
            if age < dt.timedelta(seconds=self.claim_grace_seconds):
                self.repo.release_claim(row.id)
                return None
        return row

    def _resolve_chat_id(self, row: TelegramDelivery) -> int | None:
        if row.chat_id is not None:
            return row.chat_id
        with self.database.session() as session:
            conversation = session.get(Conversation, row.conversation_id)
            meta = dict(conversation.meta or {}) if conversation is not None else {}
        chat_id = meta.get("chat_id") or meta.get("user_id") or self.default_chat_id
        return int(chat_id) if chat_id else None

    def _row_text(self, row: TelegramDelivery) -> str | None:
        if row.kind == DELIVERY_KIND_TURN_ERROR:
            return (row.meta or {}).get("error") or "Неизвестная ошибка исполнения"
        if row.message_id is None:
            return None
        with self.database.session() as session:
            message = session.scalars(
                select(Message).where(Message.id == row.message_id)
            ).one_or_none()
        return message.text if message is not None else None

    def _render_header(self, row: TelegramDelivery) -> str | None:
        meta = row.meta or {}
        if row.kind == DELIVERY_KIND_API_QUESTION:
            return api_question_header(meta.get("api_caller"))
        if row.kind == DELIVERY_KIND_REPLY:
            if meta.get("input_source") == INPUT_SOURCE_API:
                return "💬 Ответ секретаря (вопрос был через API)"
            return None
        if row.kind == DELIVERY_KIND_TURN_ERROR:
            return "⚠️ Ошибка исполнения"
        return None

    def _foreign_plate(self, row: TelegramDelivery) -> str | None:
        if self.active_context_probe is None:
            return None
        try:
            active = self.active_context_probe() or {}
        except Exception:
            return None
        if active.get("conversation_id") == row.conversation_id:
            return None
        return render_foreign_context_plate(
            active_task_number=active.get("task_number"),
            active_conversation_id=active.get("conversation_id"),
        )

    async def _send_parts(
        self,
        row: TelegramDelivery,
        chat_id: int,
        parts: list[tuple[str, Any]],
        keyboard: Any,
    ) -> None:
        last_text_index = max(
            (index for index, (kind, _) in enumerate(parts) if kind == "text"),
            default=None,
        )
        for index, (kind, content) in enumerate(parts):
            if index < row.parts_sent:
                continue
            if kind == "text":
                message_id = await send_markdown(
                    self.bot,
                    chat_id,
                    content,
                    keyboard=keyboard if index == last_text_index else None,
                )
            elif kind == "document":
                message = await self.bot.send_document(
                    chat_id=chat_id,
                    document=BufferedInputFile(
                        content.encode("utf-8"), filename="ответ_секретаря.md"
                    ),
                    caption=with_conversation_footer(
                        "📄 Ответ секретаря", row.conversation_id
                    ),
                    reply_markup=keyboard,
                )
                message_id = message.message_id
            else:
                message_id = await self._send_file(row, chat_id, content, keyboard)
            self.repo.mark_part_sent(
                row.id, part_index=index, telegram_message_id=message_id
            )
            self.repo.record_message_link(
                chat_id=chat_id,
                telegram_message_id=message_id,
                conversation_id=row.conversation_id,
                message_id=row.message_id,
                turn_id=row.turn_id,
                task_id=(row.meta or {}).get("task_id"),
                origin=MESSAGE_ORIGIN_BOT,
            )

    async def _send_file(
        self, row: TelegramDelivery, chat_id: int, target: str, keyboard: Any
    ) -> int:
        path = (
            self.knowledge_base.resolve_file(target)
            if self.knowledge_base is not None
            else None
        )
        if path is None or not path.is_file():
            return await send_markdown(
                self.bot,
                chat_id,
                with_conversation_footer(
                    f"⚠️ Документ не найден: {target}", row.conversation_id
                ),
            )
        message = await self.bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(path),
            caption=with_conversation_footer(f"📄 {path.name}", row.conversation_id),
            reply_markup=keyboard,
        )
        return message.message_id

    def _handle_send_error(self, row: TelegramDelivery, error: Exception) -> str:
        """A network failure has an unknown outcome: requeue and retry.

        A definite Telegram API rejection (bad request, blocked chat) marks the
        row failed so it cannot loop forever; the error stays on the row for
        diagnostics. Everything else — timeouts, connection losses — releases
        the claim: Telegram may already have the part, so the retry may resend
        it once. That at-least-once window is inherent and logged explicitly.
        """
        from aiogram.exceptions import TelegramBadRequest

        definite_rejection = isinstance(error, TelegramBadRequest)
        send_outcome = "failed" if definite_rejection else "unknown"
        logger.warning(
            "telegram delivery send error",
            extra={
                "event": "telegram_delivery_send_error",
                "conversation_id": row.conversation_id,
                "delivery_id": row.id,
                "kind": row.kind,
                "outcome": send_outcome,
                "error_type": type(error).__name__,
            },
        )
        if definite_rejection:
            self.repo.mark_failed(row.id, str(error))
            return "failed"
        self.repo.release_claim(row.id)
        return "released"
