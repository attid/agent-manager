"""Shared pytest fixtures for the storage test-suite.

All tests run against real files under a temporary directory, never against an
in-memory SQLite database, so WAL, migrations and cross-connection behaviour
are exercised for real.
"""

from __future__ import annotations

import pathlib

import pytest
from alembic import command
from alembic.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def alembic_config(db_path: pathlib.Path) -> Config:
    """Return an Alembic config bound to ``db_path``."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def upgrade_to_head(db_path: pathlib.Path) -> None:
    """Run the real migration chain against ``db_path``."""
    command.upgrade(alembic_config(db_path), "head")


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Path to a not-yet-created file database."""
    return tmp_path / "agent_manager_test.db"


@pytest.fixture
def migrated_db(db_path: pathlib.Path) -> pathlib.Path:
    """A file database migrated to the current head revision."""
    upgrade_to_head(db_path)
    return db_path
