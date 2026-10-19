"""Alembic environment for the Agent Manager SQLite database.

The database URL is resolved from, in order:

1. the ``sqlalchemy.url`` Alembic option (for example set programmatically in
   tests or with ``-x sqlalchemy.url=...``);
2. the ``AGENT_MANAGER_DB_URL`` environment variable.

SQLite pragmas (foreign keys, bounded busy timeout) are applied to every
connection, and the file is switched to WAL journal mode before migrations
run.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy import pool

from agent_manager.storage.database import configure_sqlite_engine
from agent_manager.storage.models import Base

config = context.config

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        url = os.environ.get("AGENT_MANAGER_DB_URL")
    if not url:
        raise RuntimeError(
            "No database URL configured. Set the 'sqlalchemy.url' Alembic "
            "option or the AGENT_MANAGER_DB_URL environment variable."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the file database and run migrations in a transaction."""
    connectable = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        future=True,
    )
    configure_sqlite_engine(connectable, enable_wal=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
