# Structured logging and API error resilience

Agent Manager uses the standard Python `logging` package plus `contextvars`
for diagnostic correlation. There is no separate logging framework and no
global mutable "current request" object.

## Modules

| Module | Responsibility |
| --- | --- |
| `agent_manager.logging` | Context variables, context binding, formatters and root logging configuration. |
| `agent_manager.api.app` | Request correlation middleware, request start/completion logs and the unhandled-exception boundary. |

## Diagnostic context

The canonical context fields are:

| Field | Meaning |
| --- | --- |
| `request_id` | Correlation id for one HTTP request. |
| `conversation_id` | Durable conversation being processed. |
| `turn_id` | Durable turn or execution attempt. |
| `task_id` | Task record being worked on. |
| `agent_id` | Agent profile or logical agent. |

Bind fields with `bind_log_context`:

```python
from agent_manager.logging import bind_log_context, get_log_context

with bind_log_context(request_id="req-42", task_id="task-9"):
    assert get_log_context() == {
        "request_id": "req-42",
        "task_id": "task-9",
    }
    logger.info("starting work")
# The previous values are restored here.
```

`bind_log_context` is a context manager, so it is safe to nest. Each field is
stored in its own `ContextVar`; on exit the previous values are restored in
reverse order, including when the block raises. Unknown field names raise
`ValueError` rather than silently disappearing. Binding a field to `None`
removes it from the active context while the block runs.

Because `contextvars` follow async tasks and the thread-pool copies used by
synchronous FastAPI endpoints, context bound by the request middleware is
available to code deeper in the request without passing logger objects or ids
through every function signature.

Every log record created after `agent_manager.logging` is imported carries:

* `record.log_context` — a snapshot of the active fields at record creation;
* the active fields as direct record attributes (`record.request_id`,
  `record.task_id`, and so on, with `None` when unbound).

The record factory is installed once at import. `configure_logging` reinstalls
it idempotently if another component has replaced the global factory.

## Text format

`StructuredFormatter` (also exported as `ContextFormatter`) renders one line
with timestamp, level, logger, active context, structured extra fields and the
message:

```text
timestamp=2025-10-17T15:20:30.123+00:00 level=INFO logger=agent_manager.api.app request_id=req-42 event=request_start method=GET path=/health message=GET /health request started
```

Timestamps are ISO-8601 UTC values with millisecond precision. Only active
context fields are included. Exception and stack information, when present,
follow the line as `exception=` / `stack=` blocks.

## JSON format

`JsonFormatter` (also exported as `JSONFormatter`) emits one JSON object per
record:

```json
{
  "timestamp": "2025-10-17T15:20:30.123+00:00",
  "level": "INFO",
  "logger": "agent_manager.api.app",
  "message": "GET /health request completed status_code=200 duration_ms=1.234",
  "context": {"request_id": "req-42"},
  "request_id": "req-42",
  "event": "request_complete",
  "status_code": 200,
  "duration_ms": 1.234
}
```

Context fields appear both at the top level for simple queries and inside the
`context` object for structured consumers. Exception tracebacks are included
under `exception`.

## Configuring root logging

```python
import logging

from agent_manager.logging import configure_logging

configure_logging(
    level="INFO",              # name or numeric level
    stream=None,               # defaults to sys.stderr
    log_file="/var/log/agent-manager.jsonl",
    json_format=True,
)
```

`configure_logging` is idempotent: repeated calls replace only the handlers it
created itself, so the managed handler count stays at one. Handlers owned by
another component are preserved. In particular, pytest's `caplog` handler
keeps working, and the suite does not need to disable logging to inspect
records.

Pass `json_format=False` (the default) for the structured text formatter. A
file handler writes UTF-8 and can be combined with the console handler.

The application factory does not reconfigure root logging. The host process
(uvicorn, a deployment wrapper or tests) owns logging policy and can call
`configure_logging` once at startup.

## Request correlation

`create_app` installs two ASGI middlewares around every HTTP route:

1. `RequestCorrelationMiddleware`
2. `UnhandledExceptionMiddleware` (outside the correlation middleware)

For each request the correlation middleware:

* reads `X-Request-ID` from the incoming headers;
* generates a fresh UUID when the header is absent or blank;
* stores the id in request state and binds `request_id` in the log context for
  the entire request lifecycle;
* logs `METHOD path request started`;
* logs `METHOD path request completed status_code=... duration_ms=...`;
* writes `X-Request-ID` into the outgoing response headers, replacing any
  existing value with the id that was actually used.

Both request log records also carry structured `event`, `method`, `path`,
`status_code` and `duration_ms` extra fields. The same id is available to
endpoint and repository logs through the bound context.

## Unhandled exception behaviour

An unexpected `Exception` that escapes a route handler is caught by
`UnhandledExceptionMiddleware` before Starlette's outer error middleware can
re-raise it. The middleware:

* logs the exception at `ERROR` level with the full traceback, method, path,
  request id and diagnostic context;
* returns HTTP `500` with the JSON body

  ```json
  {"detail": "Internal server error", "request_id": "..."}
  ```

* preserves the `X-Request-ID` response header;
* does not re-raise before the response is sent, so the process remains alive
  and subsequent requests are served normally.

If a response has already started, it cannot be replaced; the exception is
logged and re-raised so the server can close the failed response. A fallback
`Exception` handler is also registered on the FastAPI application for
exceptions outside the middleware path. Handled client errors such as `404`
and `422`, and expected domain conflicts such as `409`, are not affected:
they continue to use their normal FastAPI responses and still receive the
correlation header.

## Testing notes

`tests/test_logging.py` covers context binding, nested restoration, text and
JSON formatting, exceptions in formatted output, and idempotent root
configuration that preserves other handlers.

`tests/test_api_resilience.py` covers request-id propagation, generated UUIDs,
request start/completion records with duration, context propagation into
endpoint logs, the 500 response contract, logged tracebacks, and successful
requests after a handler failure.
