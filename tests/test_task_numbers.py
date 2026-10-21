"""Human references preserve internal task identity and concurrent allocation."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

from alembic import command

from fastapi.testclient import TestClient

from kuzya.api import create_app
from kuzya.storage import Database, TaskRepository
from .conftest import alembic_config


def test_task_numbers_survive_archive_and_resolve_to_internal_id(migrated_db: Path):
    db = Database.from_path(migrated_db)
    try:
        repo = TaskRepository(db)
        first = repo.create(title="First")
        second = repo.create(title="Second")
        assert (first.number, second.number) == (1, 2)
        assert repo.get("2").id == second.id
        assert repo.get("#2").id == second.id
        assert repo.get(second.id).number == 2
        assert repo.update("2", status="archived").id == second.id
        assert repo.create(title="Third").number == 3
    finally:
        db.dispose()


def test_concurrent_task_numbers_and_imports(migrated_db: Path):
    db = Database.from_path(migrated_db)
    try:
        repo = TaskRepository(db)
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = list(pool.map(lambda i: repo.create(title=f"Task {i}"), range(8)))
        assert sorted(t.number for t in tasks) == list(range(1, 9))
        repo.import_tasks([{"id": "imported", "title": "Imported"}])
        repo.import_tasks([{"id": "imported", "title": "Repeat"}])
        assert repo.get("imported").number == 9
        assert repo.create(title="After import").number == 10
    finally:
        db.dispose()


def test_api_can_open_and_update_by_human_number(migrated_db: Path):
    with TestClient(create_app(f"sqlite:///{migrated_db}")) as client:
        first = client.post("/tasks", json={"title": "Human reference"}).json()
        assert first["number"] == 1
        assert first["id"] != "1"
        assert client.get("/tasks/1").json()["id"] == first["id"]
        updated = client.patch("/tasks/1", json={"title": "Updated"})
        assert updated.status_code == 200
        assert updated.json()["id"] == first["id"]
        assert client.get("/tasks").json()[0]["number"] == 1


def test_number_migration_keeps_ids_and_assigns_creation_order(db_path: Path):
    config = alembic_config(db_path)
    command.upgrade(config, "0009_analysis")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO tasks(id,title,created_at) VALUES (?,?,?)",
            ("second-uuid", "Second", "2026-10-19 10:00:00"),
        )
        connection.execute(
            "INSERT INTO tasks(id,title,created_at) VALUES (?,?,?)",
            ("first-uuid", "First", "2026-10-18 10:00:00"),
        )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT id,number FROM tasks ORDER BY number").fetchall() == [
            ("first-uuid", 1), ("second-uuid", 2),
        ]
        connection.execute("INSERT INTO tasks(id,title) VALUES ('legacy-insert','Legacy')")
        assert connection.execute("SELECT number FROM tasks WHERE id='legacy-insert'").fetchone() == (3,)
