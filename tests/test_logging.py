"""Acceptance tests for context-aware logging and formatters."""

from __future__ import annotations

import io
import json
import logging

import pytest

from agent_manager.logging import (
    ContextFormatter,
    JSONFormatter,
    LOG_CONTEXT_FIELDS,
    StructuredFormatter,
    bind_log_context,
    configure_logging,
    get_log_context,
)

_MANAGED_HANDLER_ATTRIBUTE = "_agent_manager_managed_handler"


@pytest.fixture
def isolated_logger() -> logging.Logger:
    """Return a logger that does not propagate into the root test capture."""

    logger = logging.getLogger("agent_manager.tests.logging")
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.handlers = []
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield logger
    finally:
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def test_bind_log_context_sets_and_restores_fields() -> None:
    assert get_log_context() == {}

    with bind_log_context(request_id="req-1", task_id="task-1"):
        assert get_log_context() == {
            "request_id": "req-1",
            "task_id": "task-1",
        }

    assert get_log_context() == {}


def test_nested_context_bindings_restore_previous_values() -> None:
    with bind_log_context(
        request_id="outer-request",
        conversation_id="conversation-1",
    ):
        assert get_log_context() == {
            "request_id": "outer-request",
            "conversation_id": "conversation-1",
        }

        with bind_log_context(
            request_id="inner-request",
            turn_id="turn-1",
        ):
            assert get_log_context() == {
                "request_id": "inner-request",
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
            }

        assert get_log_context() == {
            "request_id": "outer-request",
            "conversation_id": "conversation-1",
        }

    assert get_log_context() == {}


def test_context_is_restored_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with bind_log_context(agent_id="agent-1"):
            assert get_log_context() == {"agent_id": "agent-1"}
            raise RuntimeError("boom")

    assert get_log_context() == {}


def test_unknown_context_field_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown_field"):
        with bind_log_context(unknown_field="value"):
            pass

    assert get_log_context() == {}


def test_structured_formatter_contains_context_fields_and_message(
    isolated_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    isolated_logger.addHandler(handler)

    with bind_log_context(request_id="req-42", task_id="task-7"):
        isolated_logger.info("processed %s", "payload")

    output = stream.getvalue()
    assert "timestamp=" in output
    assert "level=INFO" in output
    assert "logger=agent_manager.tests.logging" in output
    assert "request_id=req-42" in output
    assert "task_id=task-7" in output
    assert "processed payload" in output
    assert "conversation_id=" not in output


def test_json_formatter_emits_one_structured_object(
    isolated_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    isolated_logger.addHandler(handler)

    with bind_log_context(request_id="req-json", agent_id="agent-1"):
        isolated_logger.warning("json message")

    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "agent_manager.tests.logging"
    assert payload["message"] == "json message"
    assert payload["timestamp"]
    assert payload["request_id"] == "req-json"
    assert payload["agent_id"] == "agent-1"
    assert payload["context"] == {
        "request_id": "req-json",
        "agent_id": "agent-1",
    }


def test_json_formatter_includes_exception_traceback(
    isolated_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    isolated_logger.addHandler(handler)

    try:
        raise ValueError("bad state")
    except ValueError:
        isolated_logger.exception("failed to process")

    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "failed to process"
    assert "Traceback" in payload["exception"]
    assert "ValueError: bad state" in payload["exception"]


def test_records_expose_context_as_attributes(
    isolated_logger: logging.Logger,
) -> None:
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    isolated_logger.addHandler(CaptureHandler())
    with bind_log_context(request_id="req-attr", conversation_id="conv-1"):
        isolated_logger.info("captured")

    assert len(records) == 1
    record = records[0]
    assert record.request_id == "req-attr"
    assert record.conversation_id == "conv-1"
    assert record.log_context == {
        "request_id": "req-attr",
        "conversation_id": "conv-1",
    }


def test_extra_can_override_a_bound_context_field(
    isolated_logger: logging.Logger,
) -> None:
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    isolated_logger.addHandler(CaptureHandler())
    with bind_log_context(request_id="context-request"):
        isolated_logger.info(
            "overridden",
            extra={"request_id": "extra-request"},
        )

    assert records[0].request_id == "extra-request"


def test_formatter_aliases_expose_the_expected_behaviour() -> None:
    assert ContextFormatter is StructuredFormatter
    assert JSONFormatter.__name__ == "JsonFormatter"
    assert set(LOG_CONTEXT_FIELDS) == {
        "request_id",
        "conversation_id",
        "turn_id",
        "task_id",
        "agent_id",
        "worker_id",
    }


def test_worker_id_is_a_supported_context_field(
    isolated_logger: logging.Logger,
) -> None:
    """The background dispatcher binds ``worker_id`` for every record.

    This extends the correlation-field contract with the dispatcher's worker
    identity; the formatters and record attributes must carry it exactly like
    the pre-existing fields.
    """

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    isolated_logger.addHandler(handler)

    with bind_log_context(
        worker_id="dispatcher-abc",
        conversation_id="conv-1",
    ):
        isolated_logger.info("claimed")

    output = stream.getvalue()
    assert "worker_id=dispatcher-abc" in output
    assert "conversation_id=conv-1" in output


def test_configure_logging_is_idempotent_and_preserves_other_handlers() -> None:
    root = logging.getLogger()
    previous_level = root.level
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    stream = io.StringIO()

    try:
        configure_logging(level=logging.INFO, stream=stream)
        first_managed = [
            handler
            for handler in root.handlers
            if getattr(handler, _MANAGED_HANDLER_ATTRIBUTE, False)
        ]
        assert len(first_managed) == 1
        assert sentinel in root.handlers

        configure_logging(level=logging.DEBUG, stream=stream)
        second_managed = [
            handler
            for handler in root.handlers
            if getattr(handler, _MANAGED_HANDLER_ATTRIBUTE, False)
        ]
        assert len(second_managed) == 1
        assert sentinel in root.handlers
        assert root.level == logging.DEBUG
    finally:
        for handler in list(root.handlers):
            if getattr(handler, _MANAGED_HANDLER_ATTRIBUTE, False):
                root.removeHandler(handler)
                handler.close()
        if sentinel in root.handlers:
            root.removeHandler(sentinel)
        root.setLevel(previous_level)
