"""Engine and connection management for the file-backed SQLite database.

Each :class:`Database` owns one SQLAlchemy engine bound to a single SQLite
file. A connection (and the SQLAlchemy :class:`~sqlalchemy.orm.Session` built
on it) belongs to the execution context that opened it; it must not be shared
between threads. Independent contexts open independent connections.

Every SQLite connection is configured with:

* ``PRAGMA foreign_keys=ON``
* ``PRAGMA busy_timeout=<bounded milliseconds>``

WAL journal mode is a persistent property of the database file and is enabled
once when the :class:`Database` is created (or by the Alembic migration
environment). WAL lets readers proceed while a writer holds an open
transaction; writers still serialize.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

DEFAULT_BUSY_TIMEOUT_MS = 5000


def _apply_connection_pragmas(
    dbapi_connection: sqlite3.Connection, busy_timeout_ms: int
) -> None:
    """Set per-connection SQLite pragmas.

    ``foreign_keys`` and ``busy_timeout`` are per-connection settings, so they
    must be re-applied to every physical connection, not only the first one.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    finally:
        cursor.close()


def configure_sqlite_engine(
    engine: Engine,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    enable_wal: bool = False,
) -> None:
    """Attach SQLite behaviour to ``engine``.

    Registers a ``connect`` listener that applies ``foreign_keys`` and
    ``busy_timeout`` on every new DBAPI connection. When ``enable_wal`` is
    true, also switches the database file into WAL journal mode once.

    No-op for non-SQLite engines.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(
        dbapi_connection: Any, _connection_record: Any
    ) -> None:
        if isinstance(dbapi_connection, sqlite3.Connection):
            _apply_connection_pragmas(dbapi_connection, busy_timeout_ms)

    if enable_wal:
        # ``PRAGMA journal_mode`` requires no open transaction; use a raw
        # DBAPI connection so nothing wraps it in a SQLAlchemy transaction.
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.fetchall()
            finally:
                cursor.close()
        finally:
            raw.close()


class Database:
    """Owns one SQLite engine and its session factory.

    Args:
        url: SQLAlchemy database URL, for example ``sqlite:////data/tasks.db``.
        busy_timeout_ms: Bounded time SQLite waits for a locked writer before
            raising ``database is locked``.
        echo: Forwarded to SQLAlchemy for SQL logging.
        enable_wal: Switch the file into WAL mode when the object is created.

    The engine uses :class:`~sqlalchemy.pool.NullPool`, so each session gets a
    fresh physical connection. This keeps a connection owned by the execution
    context that opened it and avoids handing a connection to another thread.
    WAL mode is required for file databases; in-memory databases are not
    supported because each connection would see a different database.
    """

    def __init__(
        self,
        url: str,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        echo: bool = False,
        enable_wal: bool = True,
    ) -> None:
        if url in (":memory:", "sqlite://", "sqlite:///:memory:"):
            raise ValueError(
                "Database requires a file-backed SQLite URL; in-memory "
                "databases cannot share state across connections."
            )
        self.url = url
        self.busy_timeout_ms = busy_timeout_ms
        self.engine = create_engine(
            url,
            echo=echo,
            future=True,
            poolclass=NullPool,
            connect_args={
                "check_same_thread": False,
                "timeout": busy_timeout_ms / 1000.0,
            },
        )
        configure_sqlite_engine(
            self.engine,
            busy_timeout_ms=busy_timeout_ms,
            enable_wal=enable_wal,
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            future=True,
        )

    @classmethod
    def from_path(
        cls, path: str | os.PathLike[str], **kwargs: Any
    ) -> "Database":
        """Build a :class:`Database` for a filesystem path."""
        return cls(f"sqlite:///{Path(path).resolve()}", **kwargs)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a session that commits on success and rolls back on error.

        The transaction is intentionally short-lived: callers perform one
        logical operation and exit, so writers do not hold the SQLite write
        lock longer than necessary.
        """
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Close all pooled connections owned by the engine."""
        self.engine.dispose()
