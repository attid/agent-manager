"""Canonical context-aware logging for Kuzya.

The module keeps the logging setup deliberately small and standard:

* :data:`LOG_CONTEXT_FIELDS` names the diagnostic correlation fields.
* :func:`bind_log_context` binds one or more fields for a block and restores
  the previous values on exit, which makes nested request/execution scopes
  safe.
* :class:`StructuredFormatter` renders a compact ``key=value`` line.
* :class:`JsonFormatter` renders the same record as one JSON object.
* :func:`configure_logging` installs idempotent root handlers without removing
  handlers owned by another component (for example pytest's ``caplog``).

Context is stored in :mod:`contextvars`, so it follows async tasks and the
thread-pool copies used by synchronous FastAPI endpoints. Every log record
created after this module is imported carries the active context fields as
record attributes and as ``record.log_context``. The record factory is
installed once at import time and is reinstalled idempotently by
:func:`configure_logging`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, TextIO

__all__ = [
    "ContextFilter",
    "ContextFormatter",
    "JSONFormatter",
    "JsonFormatter",
    "LOG_CONTEXT_FIELDS",
    "StructuredFormatter",
    "bind_log_context",
    "configure_logging",
    "get_log_context",
    "install_context_record_factory",
]

#: Diagnostic correlation fields carried by every log record.
LOG_CONTEXT_FIELDS: tuple[str, ...] = (
    "request_id",
    "conversation_id",
    "turn_id",
    "task_id",
    "agent_id",
    "worker_id",
)

_CONTEXT_VARS: dict[str, ContextVar[Any]] = {
    field: ContextVar(f"kuzya_log_{field}", default=None)
    for field in LOG_CONTEXT_FIELDS
}

_CONFIG_LOCK = threading.RLock()
_MANAGED_HANDLER_ATTRIBUTE = "_kuzya_managed_handler"

# Attributes every LogRecord already has (plus the fixed JSON field names).
# They are not treated as structured ``extra`` values by the formatters.
_RESERVED_RECORD_ATTRIBUTES = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "level",
        "levelname",
        "levelno",
        "lineno",
        "log_context",
        "logger",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        "timestamp",
        "exception",
    }
)


def get_log_context() -> dict[str, Any]:
    """Return the active context fields as a new dictionary.

    Fields that were never bound, or that were explicitly bound to ``None``,
    are omitted. The returned mapping is a snapshot and can be modified by the
    caller without affecting the context.
    """

    context: dict[str, Any] = {}
    for field, variable in _CONTEXT_VARS.items():
        value = variable.get()
        if value is not None:
            context[field] = value
    return context


@contextmanager
def bind_log_context(**kwargs: Any) -> Iterator[None]:
    """Bind logging context fields for the duration of a block.

    Args:
        **kwargs: Context fields to bind. Supported keys are the names in
            :data:`LOG_CONTEXT_FIELDS`.

    Yields:
        ``None``.

    Raises:
        ValueError: If an unknown context field is supplied.

    The previous values are restored on exit, including on exceptions, so
    nested bindings behave like a stack::

        with bind_log_context(request_id="req-1"):
            with bind_log_context(task_id="task-9"):
                ...  # both fields are active
            ...  # only request_id is active again
    """

    unknown = set(kwargs) - set(LOG_CONTEXT_FIELDS)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown log context field(s): {names}")

    tokens: list[tuple[ContextVar[Any], Token[Any]]] = []
    try:
        for field, value in kwargs.items():
            variable = _CONTEXT_VARS[field]
            tokens.append((variable, variable.set(value)))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def _record_context(record: logging.LogRecord) -> dict[str, Any]:
    """Return the context that was active when ``record`` was created."""

    stored = getattr(record, "log_context", None)
    if not isinstance(stored, Mapping):
        stored = get_log_context()

    context: dict[str, Any] = {}
    for field in LOG_CONTEXT_FIELDS:
        value = getattr(record, field, None)
        if value is None:
            value = stored.get(field)
        context[field] = value
    return context


def _format_value(value: Any) -> str:
    """Render one structured value for the text formatter."""

    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _record_extras(record: logging.LogRecord) -> Iterator[tuple[str, Any]]:
    """Yield non-standard record attributes as structured fields."""

    for key, value in record.__dict__.items():
        if (
            key in _RESERVED_RECORD_ATTRIBUTES
            or key in LOG_CONTEXT_FIELDS
            or value is None
        ):
            continue
        yield key, value


class ContextFilter(logging.Filter):
    """Attach active context fields to records that do not have them yet."""

    def filter(self, record: logging.LogRecord) -> bool:
        stored = getattr(record, "log_context", None)
        if not isinstance(stored, Mapping):
            stored = get_log_context()
            record.log_context = stored
        for field in LOG_CONTEXT_FIELDS:
            if not hasattr(record, field):
                setattr(record, field, stored.get(field))
        return True


class StructuredFormatter(logging.Formatter):
    """Render one record as a compact structured text line.

    Example::

        timestamp=2025-10-17T15:20:30.123+00:00 level=INFO \\
        logger=kuzya.api.app request_id=req-1 \\
        message=GET /health request completed status_code=200 duration_ms=1.234
    """

    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        moment = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return moment.isoformat(timespec="milliseconds")

    def format(self, record: logging.LogRecord) -> str:
        context = _record_context(record)
        parts = [
            f"timestamp={self.formatTime(record)}",
            f"level={record.levelname}",
            f"logger={record.name}",
        ]
        for field in LOG_CONTEXT_FIELDS:
            value = context.get(field)
            if value is not None:
                parts.append(f"{field}={_format_value(value)}")
        for key, value in _record_extras(record):
            parts.append(f"{key}={_format_value(value)}")
        parts.append(f"message={record.getMessage()}")

        rendered = " ".join(parts)
        if record.exc_info:
            rendered += "\nexception=" + self.formatException(record.exc_info)
        if record.stack_info:
            rendered += "\nstack=" + self.formatStack(record.stack_info)
        return rendered


class JsonFormatter(logging.Formatter):
    """Render one record as a single JSON object.

    Context fields appear both as top-level keys (convenient for log search)
    and in the nested ``context`` object (convenient for structured
    consumers). Timestamps are ISO-8601 UTC values.
    """

    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        moment = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return moment.isoformat(timespec="milliseconds")

    def format(self, record: logging.LogRecord) -> str:
        context = _record_context(record)
        active_context = {
            field: value
            for field, value in context.items()
            if value is not None
        }
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "context": active_context,
        }
        payload.update(active_context)
        for key, value in _record_extras(record):
            payload.setdefault(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


# Friendly aliases for callers that prefer explicit formatter names.
ContextFormatter = StructuredFormatter
JSONFormatter = JsonFormatter


class _ContextLogRecord(logging.LogRecord):
    """LogRecord variant that exposes bound context as attributes.

    The context fields are properties instead of pre-populated instance
    attributes. ``Logger.makeRecord`` therefore still allows a caller to
    override a field through ``extra=...`` without raising, while readers can
    use ``record.request_id`` (and friends) as usual.
    """

    def _active_context(self) -> Mapping[str, Any]:
        context = self.__dict__.get("log_context")
        if isinstance(context, Mapping):
            return context
        return {}

    def _context_field(self, field: str) -> Any:
        if field in self.__dict__:
            return self.__dict__[field]
        return self._active_context().get(field)

    @property
    def request_id(self) -> Any:
        return self._context_field("request_id")

    @request_id.setter
    def request_id(self, value: Any) -> None:
        self.__dict__["request_id"] = value

    @property
    def conversation_id(self) -> Any:
        return self._context_field("conversation_id")

    @conversation_id.setter
    def conversation_id(self, value: Any) -> None:
        self.__dict__["conversation_id"] = value

    @property
    def turn_id(self) -> Any:
        return self._context_field("turn_id")

    @turn_id.setter
    def turn_id(self, value: Any) -> None:
        self.__dict__["turn_id"] = value

    @property
    def task_id(self) -> Any:
        return self._context_field("task_id")

    @task_id.setter
    def task_id(self, value: Any) -> None:
        self.__dict__["task_id"] = value

    @property
    def agent_id(self) -> Any:
        return self._context_field("agent_id")

    @agent_id.setter
    def agent_id(self, value: Any) -> None:
        self.__dict__["agent_id"] = value

    @property
    def worker_id(self) -> Any:
        return self._context_field("worker_id")

    @worker_id.setter
    def worker_id(self, value: Any) -> None:
        self.__dict__["worker_id"] = value


def _make_context_record_factory(base_factory: Any) -> Any:
    """Build a record factory that snapshots the active context."""

    if base_factory is logging.LogRecord:

        def context_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = _ContextLogRecord(*args, **kwargs)
            record.log_context = get_log_context()
            return record

    else:

        def context_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = base_factory(*args, **kwargs)
            record.log_context = get_log_context()
            return record

    return context_record_factory


_CONTEXT_RECORD_FACTORY = _make_context_record_factory(logging.LogRecord)


def install_context_record_factory() -> None:
    """Install the context-aware ``LogRecord`` factory if it is not active.

    The installation is idempotent. If another component replaced the global
    factory, the current factory is wrapped rather than discarded.
    """

    global _CONTEXT_RECORD_FACTORY

    with _CONFIG_LOCK:
        current = logging.getLogRecordFactory()
        if current is _CONTEXT_RECORD_FACTORY:
            return
        _CONTEXT_RECORD_FACTORY = _make_context_record_factory(current)
        logging.setLogRecordFactory(_CONTEXT_RECORD_FACTORY)


def _coerce_level(level: int | str) -> int:
    if isinstance(level, str):
        try:
            return logging.getLevelNamesMapping()[level.upper()]
        except KeyError as error:
            raise ValueError(f"Unknown logging level: {level!r}") from error
    return int(level)


def configure_logging(
    level: int | str = logging.INFO,
    stream: TextIO | None = None,
    log_file: str | os.PathLike[str] | None = None,
    json_format: bool = False,
) -> logging.Logger:
    """Configure the root logger idempotently.

    Args:
        level: Root level as an integer or a name such as ``"DEBUG"``.
        stream: Text stream for console output. Defaults to ``sys.stderr``.
        log_file: Optional file path for an additional UTF-8 file handler.
        json_format: Use :class:`JsonFormatter` instead of
            :class:`StructuredFormatter`.

    Returns:
        The configured root logger.

    Only handlers installed by a previous :func:`configure_logging` call are
    replaced. Handlers owned by other components - notably pytest's
    ``caplog`` handler - are left untouched, so test capture keeps working.
    """

    install_context_record_factory()
    resolved_level = _coerce_level(level)
    formatter: logging.Formatter = (
        JsonFormatter() if json_format else StructuredFormatter()
    )

    new_handlers: list[logging.Handler] = [
        logging.StreamHandler(stream if stream is not None else sys.stderr)
    ]
    if log_file is not None:
        new_handlers.append(
            logging.FileHandler(log_file, encoding="utf-8")
        )

    for handler in new_handlers:
        handler.setFormatter(formatter)
        handler.addFilter(ContextFilter())
        setattr(handler, _MANAGED_HANDLER_ATTRIBUTE, True)

    with _CONFIG_LOCK:
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, _MANAGED_HANDLER_ATTRIBUTE, False):
                root.removeHandler(handler)
                handler.close()
        for handler in new_handlers:
            root.addHandler(handler)
        root.setLevel(resolved_level)
    return root


# Every record created after this import carries the active context. This
# makes context available to external handlers (uvicorn, pytest caplog, a
# platform log shipper) even when the host application owns root logging.
install_context_record_factory()
