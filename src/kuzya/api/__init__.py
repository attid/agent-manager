"""HTTP API over the durable task storage boundary.

Public interface:
    kuzya.api.create_app -- build a FastAPI application bound to a
        file-backed SQLite database.

Importing this package does not create or migrate a database. Call
:func:`create_app` with a database URL (or set ``KUZYA_DB_URL``) and
run the Alembic migration first.
"""

from .app import create_app

__all__ = ["create_app"]
