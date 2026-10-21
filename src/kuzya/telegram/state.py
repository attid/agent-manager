"""Durable SQLite storage for Telegram user state and FIFO message queues."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
import sqlite3
import uuid
from typing import Any


def utcnow_str() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclasses.dataclass(frozen=True)
class UserState:
    user_id: int
    mode: str = "idle"  # "idle", "new_task", "talk", "task"
    active_task_id: str | None = None
    active_conversation_id: str | None = None
    talk_conversation_id: str | None = None
    updated_at: str = dataclasses.field(default_factory=utcnow_str)


@dataclasses.dataclass(frozen=True)
class QueuedMessage:
    id: str
    user_id: int
    chat_id: int
    telegram_message_id: int
    status: str  # "pending", "processing", "completed", "cancelled", "failed"
    context_mode: str
    context_task_id: str | None
    context_conversation_id: str | None
    text: str
    media_type: str = "text"
    media_path: str | None = None
    status_message_id: int | None = None
    created_at: str = dataclasses.field(default_factory=utcnow_str)
    updated_at: str = dataclasses.field(default_factory=utcnow_str)


class UserStateStorage:
    """SQLite-backed persistent store for Telegram session state and message queue."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_states (
                    user_id INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL DEFAULT 'idle',
                    active_task_id TEXT,
                    active_conversation_id TEXT,
                    talk_conversation_id TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_queue (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    context_mode TEXT NOT NULL,
                    context_task_id TEXT,
                    context_conversation_id TEXT,
                    text TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT 'text',
                    media_path TEXT,
                    status_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_message_queue_status_created
                ON message_queue (status, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_message_queue_conv_status
                ON message_queue (context_conversation_id, status)
                """
            )

    # -- User State --

    def get_user_state(self, user_id: int) -> UserState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_states WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                return UserState(user_id=user_id)
            return UserState(
                user_id=row["user_id"],
                mode=row["mode"],
                active_task_id=row["active_task_id"],
                active_conversation_id=row["active_conversation_id"],
                talk_conversation_id=row["talk_conversation_id"],
                updated_at=row["updated_at"],
            )

    def set_user_mode(
        self,
        user_id: int,
        mode: str,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
    ) -> UserState:
        now = utcnow_str()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_states (user_id, mode, active_task_id, active_conversation_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    mode = excluded.mode,
                    active_task_id = excluded.active_task_id,
                    active_conversation_id = excluded.active_conversation_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, mode, task_id, conversation_id, now),
            )
        return self.get_user_state(user_id)

    def set_talk_conversation(
        self, user_id: int, conversation_id: str
    ) -> None:
        now = utcnow_str()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_states (user_id, mode, talk_conversation_id, updated_at)
                VALUES (?, 'talk', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    talk_conversation_id = excluded.talk_conversation_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, conversation_id, now),
            )

    # -- Queue --

    def enqueue_message(
        self,
        *,
        user_id: int,
        chat_id: int,
        telegram_message_id: int,
        context_mode: str,
        context_task_id: str | None,
        context_conversation_id: str | None,
        text: str,
        media_type: str = "text",
        media_path: str | None = None,
        status_message_id: int | None = None,
    ) -> QueuedMessage:
        queue_id = uuid.uuid4().hex
        now = utcnow_str()
        msg = QueuedMessage(
            id=queue_id,
            user_id=user_id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            status="pending",
            context_mode=context_mode,
            context_task_id=context_task_id,
            context_conversation_id=context_conversation_id,
            text=text,
            media_type=media_type,
            media_path=media_path,
            status_message_id=status_message_id,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO message_queue (
                    id, user_id, chat_id, telegram_message_id, status,
                    context_mode, context_task_id, context_conversation_id,
                    text, media_type, media_path, status_message_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id,
                    msg.user_id,
                    msg.chat_id,
                    msg.telegram_message_id,
                    msg.status,
                    msg.context_mode,
                    msg.context_task_id,
                    msg.context_conversation_id,
                    msg.text,
                    msg.media_type,
                    msg.media_path,
                    msg.status_message_id,
                    msg.created_at,
                    msg.updated_at,
                ),
            )
        return msg

    def update_status_message_id(
        self, queue_id: str, status_message_id: int
    ) -> None:
        now = utcnow_str()
        with self._connect() as conn:
            conn.execute(
                "UPDATE message_queue SET status_message_id = ?, updated_at = ? WHERE id = ?",
                (status_message_id, now, queue_id),
            )

    def cancel_queued_message(self, queue_id: str) -> bool:
        """Cancel a message only if it is still pending in the queue."""
        now = utcnow_str()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE message_queue
                SET status = 'cancelled', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, queue_id),
            )
            return cur.rowcount == 1

    def get_queued_message(self, queue_id: str) -> QueuedMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM message_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_queued_message(row)

    def count_active_or_pending(self, conversation_id: str) -> int:
        """Count pending or processing messages for a conversation."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT count(*) FROM message_queue
                WHERE context_conversation_id = ? AND status IN ('pending', 'processing')
                """,
                (conversation_id,),
            )
            return cur.fetchone()[0]

    def is_conversation_busy(self, conversation_id: str) -> bool:
        """Check if any message is currently in processing state for this conversation."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM message_queue
                WHERE context_conversation_id = ? AND status = 'processing'
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            return row is not None

    def claim_next_pending(
        self, conversation_id: str | None = None
    ) -> QueuedMessage | None:
        """Atomically claim the next eligible pending message."""
        now = utcnow_str()
        with self._connect() as conn:
            query = """
                SELECT * FROM message_queue
                WHERE status = 'pending'
            """
            params: list[Any] = []
            if conversation_id is not None:
                query += " AND context_conversation_id = ?"
                params.append(conversation_id)
            query += " ORDER BY created_at ASC LIMIT 1"

            row = conn.execute(query, params).fetchone()
            if row is None:
                return None

            cur = conn.execute(
                """
                UPDATE message_queue
                SET status = 'processing', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, row["id"]),
            )
            if cur.rowcount == 1:
                updated_row = conn.execute(
                    "SELECT * FROM message_queue WHERE id = ?", (row["id"],)
                ).fetchone()
                if updated_row is not None:
                    return self._row_to_queued_message(updated_row)
            return None

    def set_message_status(
        self,
        queue_id: str,
        status: str,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        now = utcnow_str()
        with self._connect() as conn:
            updates = ["status = ?", "updated_at = ?"]
            params: list[Any] = [status, now]
            if conversation_id is not None:
                updates.append("context_conversation_id = ?")
                params.append(conversation_id)
            if task_id is not None:
                updates.append("context_task_id = ?")
                params.append(task_id)
            params.append(queue_id)
            conn.execute(
                f"UPDATE message_queue SET {', '.join(updates)} WHERE id = ?",
                params,
            )

    @staticmethod
    def _row_to_queued_message(row: sqlite3.Row) -> QueuedMessage:
        return QueuedMessage(
            id=row["id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            telegram_message_id=row["telegram_message_id"],
            status=row["status"],
            context_mode=row["context_mode"],
            context_task_id=row["context_task_id"],
            context_conversation_id=row["context_conversation_id"],
            text=row["text"],
            media_type=row["media_type"],
            media_path=row["media_path"],
            status_message_id=row["status_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
