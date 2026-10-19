"""Acceptance tests for API correlation and unhandled-error resilience."""

from __future__ import annotations

import logging
import pathlib
import re
import uuid

from fastapi.testclient import TestClient

from agent_manager.api import create_app


def _app(db_path: pathlib.Path):
    """Build an application for a file database path."""
    return create_app(f"sqlite:///{db_path}")


def test_request_id_is_propagated_and_request_duration_is_logged(
    migrated_db: pathlib.Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    app = _app(migrated_db)

    @app.get("/log-context")
    def log_context_endpoint() -> dict[str, bool]:
        logging.getLogger("agent_manager.tests.api").info(
            "endpoint log line"
        )
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={"X-Request-ID": "client-request-1"},
        )
        endpoint_response = client.get(
            "/log-context",
            headers={"X-Request-ID": "client-request-2"},
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "client-request-1"
    assert endpoint_response.status_code == 200
    assert endpoint_response.headers["x-request-id"] == "client-request-2"

    messages = [record.getMessage() for record in caplog.records]
    assert "GET /health request started" in messages
    completion = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("GET /health request completed")
    )
    assert "status_code=200" in completion.getMessage()
    assert re.search(r"duration_ms=\d+\.\d{3}", completion.getMessage())
    assert completion.status_code == 200
    assert isinstance(completion.duration_ms, float)
    assert completion.duration_ms >= 0
    assert completion.request_id == "client-request-1"

    endpoint_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "endpoint log line"
    )
    assert endpoint_record.request_id == "client-request-2"
    assert endpoint_record.log_context["request_id"] == "client-request-2"


def test_missing_request_id_is_generated_as_a_uuid(
    migrated_db: pathlib.Path,
) -> None:
    with TestClient(_app(migrated_db)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    parsed = uuid.UUID(request_id)
    assert parsed.version == 4
    assert str(parsed) == request_id


def test_unhandled_exception_returns_500_and_service_stays_healthy(
    migrated_db: pathlib.Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    app = _app(migrated_db)

    @app.get("/explode")
    def explode() -> None:
        raise RuntimeError("explode-boom")

    with TestClient(app) as client:
        failed = client.get(
            "/explode",
            headers={"X-Request-ID": "request-boom"},
        )
        healthy = client.get("/health")
        failed_again = client.get("/explode")

    assert failed.status_code == 500
    assert failed.json() == {
        "detail": "Internal server error",
        "request_id": "request-boom",
    }
    assert failed.headers["x-request-id"] == "request-boom"

    assert healthy.status_code == 200
    assert healthy.json() == {"status": "ok"}

    assert failed_again.status_code == 500
    assert failed_again.json()["detail"] == "Internal server error"
    assert failed_again.json()["request_id"] == failed_again.headers[
        "x-request-id"
    ]

    error_records = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and "unhandled exception" in record.getMessage()
    ]
    assert error_records
    first_error = error_records[0]
    assert first_error.exc_info is not None
    assert first_error.request_id == "request-boom"
    assert first_error.status_code == 500
    assert "RuntimeError: explode-boom" in caplog.text
    assert "Traceback" in caplog.text

    completions = [
        record
        for record in caplog.records
        if record.getMessage().startswith("GET /explode request completed")
    ]
    assert completions
    assert completions[0].status_code == 500
    assert completions[0].request_id == "request-boom"
    assert isinstance(completions[0].duration_ms, float)


def test_unhandled_exception_without_header_gets_generated_request_id(
    migrated_db: pathlib.Path,
) -> None:
    app = _app(migrated_db)

    @app.get("/explode")
    def explode() -> None:
        raise ValueError("no header")

    with TestClient(app) as client:
        response = client.get("/explode")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Internal server error"
    request_id = body["request_id"]
    uuid.UUID(request_id)
    assert response.headers["x-request-id"] == request_id
