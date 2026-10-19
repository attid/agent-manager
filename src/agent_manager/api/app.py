"""FastAPI application factory for the durable storage boundary.

The factory never creates, replaces or drops schema. The database file must
already exist and contain the ``tasks``, ``conversations``, ``messages``,
``agent_profiles``, ``turns`` and ``conversation_runtimes`` tables produced by
the Alembic migration chain; otherwise application startup fails with a clear
error telling the operator to run the migration.

An engine is created lazily inside the application lifespan and disposed when
the application stops, so importing this module never touches the database
file. Every request opens its own short-lived session through
:class:`~agent_manager.storage.repository.TaskRepository`,
:class:`~agent_manager.storage.repository.ConversationRepository` or
:class:`~agent_manager.storage.agents.AgentProfileRepository`; sessions are
never shared between requests.

The ``/agents`` endpoints are thin HTTP wrappers over the immutable versioned
agent-profile repository. They are administrative endpoints of the loopback
development prototype: they have no authentication or authorization and are
not automatically granted to an LLM or a Telegram client, and they are not a
production permission control.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as sa
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy.engine import make_url
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..logging import bind_log_context
from ..secretary import (
    TurnConversationMismatchError,
    TurnInputMessageNotFoundError,
    TurnInputRoleError,
    TurnProcessor,
)
from ..storage import (
    CORE_SYSTEM_AGENTS,
    AgentProfileNotFoundError,
    AgentProfileRepository,
    AgentProfileRevisionConflictError,
    ConversationNotFoundError,
    ConversationRepository,
    Database,
    DuplicateAgentProfileError,
    DuplicateConversationIdError,
    DuplicateTaskIdError,
    MessageConflictError,
    TaskNotFoundError,
    TaskRepository,
)
from .schemas import (
    AgentCreate,
    AgentPatch,
    AgentRead,
    ConversationCreate,
    ConversationRead,
    MessageAppendResponse,
    MessageCreate,
    MessageRead,
    TaskCreate,
    TaskImportRequest,
    TaskImportResponse,
    TaskPatch,
    TaskRead,
    TurnCreate,
    TurnRead,
    agent_to_read,
    conversation_to_read,
    import_item_to_record,
    message_append_to_response,
    message_to_read,
    task_to_read,
    turn_to_read,
)

ENV_VAR = "AGENT_MANAGER_DB_URL"

REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger(__name__)


# Tables that must already exist (via the Alembic migration chain) before the
# application accepts traffic. Checking them at startup turns an unmigrated
# database into a clear, early error instead of a later request-time 500.
_REQUIRED_TABLES = (
    "tasks",
    "conversations",
    "messages",
    "agent_profiles",
    "conversation_runtimes",
    "turns",
)

_MIGRATION_HINT = (
    "Run the migration first: "
    "AGENT_MANAGER_DB_URL={url} PYTHONPATH=src .venv/bin/alembic upgrade head"
)


def _scope_header(scope: Scope, name: str) -> str | None:
    """Return the first request header value named ``name``."""

    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def _request_state(scope: Scope) -> dict[str, object]:
    """Return the mutable request-state mapping for ``scope``."""

    state = scope.get("state")
    if not isinstance(state, dict):
        state = {}
        scope["state"] = state
    return state


def _request_id_for_scope(scope: Scope) -> str:
    """Return the request id already chosen for ``scope`` or choose one.

    An incoming ``X-Request-ID`` is preserved. Blank values and requests
    without the header receive a fresh UUID. The chosen id is stored in the
    request state so outer middleware can still use it after inner context
    bindings have been reset.

    """

    state = _request_state(scope)
    existing = state.get("request_id")
    if isinstance(existing, str) and existing:
        return existing

    header_value = _scope_header(scope, REQUEST_ID_HEADER)
    request_id = header_value.strip() if header_value else ""
    if not request_id:
        request_id = str(uuid.uuid4())
    state["request_id"] = request_id
    return request_id


def _set_response_header(
    message: Message,
    name: str,
    value: str,
) -> None:
    """Set a response header, replacing any existing occurrence."""

    raw_name = name.lower().encode("latin-1")
    headers = [
        (key, header_value)
        for key, header_value in message.get("headers", [])
        if key.lower() != raw_name
    ]
    headers.append((raw_name, value.encode("latin-1")))
    message["headers"] = headers


def _internal_error_response(request_id: str) -> JSONResponse:
    """Build the canonical 500 response for an unhandled exception."""

    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "request_id": request_id,
        },
        headers={REQUEST_ID_HEADER: request_id},
    )


def _log_unhandled_exception(
    exc: Exception,
    scope: Scope,
    request_id: str,
) -> None:
    """Log an escaping exception with its full traceback and context."""

    method = scope.get("method", "")
    path = scope.get("path", "")
    with bind_log_context(request_id=request_id):
        logger.error(
            "unhandled exception method=%s path=%s request_id=%s",
            method,
            path,
            request_id,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "event": "unhandled_exception",
                "method": method,
                "path": path,
                "status_code": 500,
            },
        )


class UnhandledExceptionMiddleware:
    """Convert unexpected route exceptions into a logged 500 response.

    This middleware sits outside the request-correlation middleware, so an
    exception that escapes a route handler is caught before Starlette's
    outer error middleware re-raises it. The process therefore stays alive
    and the next request is served normally. A response that has already
    started cannot be replaced; in that case the exception is re-raised after
    being logged so the server can close the failed response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_tracking_start(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking_start)
        except Exception as exc:
            request_id = _request_id_for_scope(scope)
            _log_unhandled_exception(exc, scope, request_id)
            if response_started:
                raise
            response = _internal_error_response(request_id)
            await response(scope, receive, send)


class BearerAuthMiddleware:
    """Validate incoming Bearer tokens against an allowed token set.

    Allows unrestricted access to `/health` and OPTIONS requests.
    If no allowed tokens are configured (empty set), all requests are allowed.
    """

    def __init__(self, app: ASGIApp, allowed_tokens: set[str]) -> None:
        self.app = app
        self.allowed_tokens = allowed_tokens

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # If no tokens are configured, pass through (open mode for tests/dev)
        if not self.allowed_tokens:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # Exclude /health and OPTIONS (CORS preflight)
        if path == "/health" or method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # Check Authorization header
        auth_header = _scope_header(scope, "authorization")
        token = ""
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

        if not token or token not in self.allowed_tokens:
            request_id = _request_id_for_scope(scope)
            logger.warning(
                "Unauthorized API access attempt method=%s path=%s request_id=%s",
                method,
                path,
                request_id,
                extra={
                    "event": "unauthorized_access",
                    "path": path,
                    "method": method,
                },
            )
            response = JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Unauthorized: invalid or missing Bearer token"},
                headers={
                    "WWW-Authenticate": "Bearer",
                    REQUEST_ID_HEADER: request_id,
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class RequestCorrelationMiddleware:
    """Bind request id context and log request start/completion."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_for_scope(scope)
        method = scope.get("method", "")
        path = scope.get("path", "")
        started_at = time.perf_counter()
        status_code: int | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                _set_response_header(
                    message,
                    REQUEST_ID_HEADER,
                    request_id,
                )
            await send(message)

        with bind_log_context(request_id=request_id):
            logger.info(
                "%s %s request started",
                method,
                path,
                extra={
                    "event": "request_start",
                    "method": method,
                    "path": path,
                },
            )
            try:
                await self.app(
                    scope,
                    receive,
                    send_with_request_id,
                )
            finally:
                duration_ms = (time.perf_counter() - started_at) * 1000
                logger.info(
                    "%s %s request completed status_code=%s "
                    "duration_ms=%.3f",
                    method,
                    path,
                    status_code if status_code is not None else 500,
                    duration_ms,
                    extra={
                        "event": "request_complete",
                        "method": method,
                        "path": path,
                        "status_code": (
                            status_code if status_code is not None else 500
                        ),
                        "duration_ms": duration_ms,
                    },
                )


def _resolve_database_url(database_url: str | None) -> str:
    """Resolve the database URL from the argument or the environment."""
    url = database_url or os.environ.get(ENV_VAR)
    if not url:
        raise RuntimeError(
            "No database URL configured. Pass database_url=... to create_app "
            f"or set the {ENV_VAR} environment variable."
        )
    return url


def _sqlite_file_path(url: str) -> Path | None:
    """Return the file path for a file-backed SQLite URL, else ``None``."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return Path(database)


def _missing_tables(database: Database) -> set[str]:
    """Return the required tables that are absent from the database."""
    inspector = sa.inspect(database.engine)
    existing = set(inspector.get_table_names())
    return {name for name in _REQUIRED_TABLES if name not in existing}


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the engine on startup and dispose it on shutdown."""
    url: str = app.state.database_url
    path = _sqlite_file_path(url)
    if path is not None and not path.exists():
        raise RuntimeError(
            f"Database file {path} does not exist. "
            + _MIGRATION_HINT.format(url=url)
        )

    database = Database(url)
    missing = _missing_tables(database)
    if missing:
        database.dispose()
        raise RuntimeError(
            f"Database {url} is missing required tables: "
            f"{', '.join(sorted(missing))}. "
            + _MIGRATION_HINT.format(url=url)
        )

    app.state.database = database
    app.state.repository = TaskRepository(database)
    app.state.conversation_repository = ConversationRepository(database)
    app.state.agent_repository = AgentProfileRepository(database)
    app.state.turn_processor = TurnProcessor(database)
    try:
        # A killed or restarted process leaves turns ``running`` with no live
        # owner. Fail them and release their reservations before serving
        # traffic so no conversation stays permanently blocked.
        result = app.state.turn_processor.reconcile_startup_orphans()
        if result.turn_ids:
            logger.warning(
                "startup reconciliation failed %d orphaned turn(s)",
                len(result.turn_ids),
                extra={
                    "event": "startup_reconciliation",
                    "reconciled_turns": len(result.turn_ids),
                    "reconciled_conversations": len(
                        result.conversation_ids
                    ),
                },
            )
        else:
            logger.info(
                "startup reconciliation found no orphaned turns",
                extra={
                    "event": "startup_reconciliation",
                    "reconciled_turns": 0,
                },
            )
        yield
    finally:
        database.dispose()


def _resolve_api_tokens(
    tokens: Sequence[str] | set[str] | None = None,
) -> set[str]:
    """Resolve allowed Bearer tokens from argument or environment."""
    if tokens is not None:
        return {t.strip() for t in tokens if t.strip()}
    raw = os.environ.get("AGENT_MANAGER_API_TOKENS", "").strip()
    if not raw:
        raw = os.environ.get("AGENT_MANAGER_API_TOKEN", "").strip()
    if not raw:
        return set()
    return {t.strip() for t in raw.split(",") if t.strip()}


def create_app(
    database_url: str | None = None,
    api_tokens: Sequence[str] | set[str] | None = None,
) -> FastAPI:
    """Build a FastAPI application bound to a file-backed SQLite database.

    Args:
        database_url: SQLAlchemy file URL, for example
            ``sqlite:////tmp/agent_manager.db``. When omitted, the
            ``AGENT_MANAGER_DB_URL`` environment variable is used. The
            database must already be migrated to head.
        api_tokens: Optional collection of allowed Bearer tokens. When supplied
            or configured via ``AGENT_MANAGER_API_TOKENS``, all endpoints
            except ``/health`` require a valid Bearer token.
    """
    url = _resolve_database_url(database_url)
    resolved_tokens = _resolve_api_tokens(api_tokens)

    app = FastAPI(title="Agent Manager", version="0.1.0", lifespan=_lifespan)
    app.state.database_url = url
    app.state.api_tokens = resolved_tokens

    # Added inner-to-outer: BearerAuth is inner to RequestCorrelation
    app.add_middleware(BearerAuthMiddleware, allowed_tokens=resolved_tokens)
    app.add_middleware(RequestCorrelationMiddleware)
    app.add_middleware(UnhandledExceptionMiddleware)

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Fallback handler for exceptions outside the resilience middleware."""

        request_id = _request_id_for_scope(request.scope)
        _log_unhandled_exception(exc, request.scope, request_id)
        return _internal_error_response(request_id)

    def get_repository(request: Request) -> TaskRepository:
        return request.app.state.repository

    def get_conversation_repository(
        request: Request,
    ) -> ConversationRepository:
        return request.app.state.conversation_repository

    def get_agent_repository(request: Request) -> AgentProfileRepository:
        return request.app.state.agent_repository

    def get_turn_processor(request: Request) -> TurnProcessor:
        return request.app.state.turn_processor

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/tasks",
        response_model=TaskRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(
        payload: TaskCreate,
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskRead:
        try:
            task = repository.create(
                title=payload.title,
                description=payload.description,
                status=payload.status,
                workspace=payload.workspace,
                metadata=payload.metadata,
                id=payload.id,
            )
        except DuplicateTaskIdError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"task id already exists: {error.task_id}",
            ) from error
        return task_to_read(task)

    @app.post("/imports/tasks", response_model=TaskImportResponse)
    def import_tasks(
        payload: TaskImportRequest,
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskImportResponse:
        """Import completed task history by stable id, idempotently.

        Replaying the same ids never duplicates or overwrites stored tasks.
        Validation rejects the whole batch before any write happens.
        """
        records = [import_item_to_record(item) for item in payload.tasks]
        result = repository.import_tasks(records)
        return TaskImportResponse(created=result.created, skipped=result.skipped)

    @app.get("/tasks", response_model=list[TaskRead])
    def list_tasks(
        status_filter: str | None = Query(default=None, alias="status"),
        workspace: str | None = Query(default=None),
        limit: int | None = Query(default=None),
        offset: int = Query(default=0),
        repository: TaskRepository = Depends(get_repository),
    ) -> list[TaskRead]:
        tasks = repository.list(
            status=status_filter,
            workspace=workspace,
            limit=limit,
            offset=offset,
        )
        return [task_to_read(task) for task in tasks]

    @app.get("/tasks/{task_id}", response_model=TaskRead)
    def get_task(
        task_id: str,
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskRead:
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task_to_read(task)

    @app.patch("/tasks/{task_id}", response_model=TaskRead)
    def patch_task(
        task_id: str,
        payload: TaskPatch,
        repository: TaskRepository = Depends(get_repository),
    ) -> TaskRead:
        try:
            task = repository.update(
                task_id=task_id,
                **payload.replacements(),
            )
        except TaskNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"task not found: {error.task_id}",
            ) from error
        return task_to_read(task)

    @app.post(
        "/conversations",
        response_model=ConversationRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_conversation(
        payload: ConversationCreate,
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> ConversationRead:
        try:
            conversation = repository.create_conversation(
                title=payload.title,
                metadata=payload.metadata,
                id=payload.id,
            )
        except DuplicateConversationIdError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "conversation id already exists: "
                    f"{error.conversation_id}"
                ),
            ) from error
        return conversation_to_read(conversation)

    @app.get("/conversations", response_model=list[ConversationRead])
    def list_conversations(
        limit: int | None = Query(default=None),
        offset: int = Query(default=0),
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> list[ConversationRead]:
        conversations = repository.list_conversations(
            limit=limit, offset=offset
        )
        return [conversation_to_read(item) for item in conversations]

    @app.get(
        "/conversations/{conversation_id}",
        response_model=ConversationRead,
    )
    def get_conversation(
        conversation_id: str,
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> ConversationRead:
        conversation = repository.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="conversation not found",
            )
        return conversation_to_read(conversation)

    @app.post(
        "/conversations/{conversation_id}/messages",
        response_model=MessageAppendResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def append_message(
        conversation_id: str,
        payload: MessageCreate,
        response: Response,
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> MessageAppendResponse:
        """Append a message; exact idempotent retries return the original.

        ``201 Created`` means a new row was written; ``200 OK`` means the
        payload matched a stored message for the same idempotency key and no
        row was written. A reused key with a different ``role``/``text``/
        ``metadata`` payload returns ``409 Conflict`` and leaves the original
        untouched. Appending to an absent conversation returns ``404``.
        """
        try:
            result = repository.append_message(
                conversation_id=conversation_id,
                role=payload.role,
                text=payload.text,
                source=payload.source,
                external_id=payload.external_id,
                metadata=payload.metadata,
            )
        except ConversationNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"conversation not found: {error.conversation_id}",
            ) from error
        except MessageConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return message_append_to_response(
            result.message, replayed=result.replayed
        )

    @app.get(
        "/conversations/{conversation_id}/messages",
        response_model=list[MessageRead],
    )
    def list_messages(
        conversation_id: str,
        limit: int | None = Query(default=None),
        offset: int = Query(default=0),
        repository: ConversationRepository = Depends(
            get_conversation_repository
        ),
    ) -> list[MessageRead]:
        if repository.get_conversation(conversation_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="conversation not found",
            )
        messages = repository.list_messages(
            conversation_id, limit=limit, offset=offset
        )
        return [message_to_read(message) for message in messages]

    @app.post(
        "/conversations/{conversation_id}/turns",
        response_model=TurnRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_turn(
        conversation_id: str,
        payload: TurnCreate,
        turn_processor: TurnProcessor = Depends(get_turn_processor),
    ) -> TurnRead:
        """Enqueue one turn for an existing inbound user message."""
        try:
            result = turn_processor.enqueue(
                conversation_id=conversation_id,
                input_message_id=payload.input_message_id,
                metadata=payload.metadata,
            )
        except TurnInputMessageNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"input message not found: {error.message_id}",
            ) from error
        except TurnConversationMismatchError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        except TurnInputRoleError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        return turn_to_read(result.turn)

    @app.get(
        "/turns/{turn_id}",
        response_model=TurnRead,
    )
    def get_turn(
        turn_id: str,
        turn_processor: TurnProcessor = Depends(get_turn_processor),
    ) -> TurnRead:
        """Return the turn status and reply reference or 404."""
        turn = turn_processor.get_turn(turn_id)
        if turn is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"turn not found: {turn_id}",
            )
        return turn_to_read(turn)

    @app.post(
        "/agents",
        response_model=AgentRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_agent(
        payload: AgentCreate,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Create revision ``1`` of an agent profile.

        A repeated ``agent_id`` returns ``409 Conflict``; when the current
        snapshot can be read its revision is included in the detail.
        """
        try:
            profile = repository.create(
                agent_id=payload.agent_id,
                name=payload.name,
                role=payload.role,
                system_prompt=payload.system_prompt,
                runner_config=payload.runner_config,
                skills=payload.skills,
                knowledge=payload.knowledge,
            )
        except DuplicateAgentProfileError as error:
            current = repository.get_current(error.agent_id)
            detail = f"agent profile already exists: {error.agent_id}"
            if current is not None:
                detail += f" (current revision {current.revision})"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            ) from error
        return agent_to_read(profile)

    @app.get("/agents", response_model=list[AgentRead])
    def list_agents(
        include_archived: bool = Query(default=False),
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> list[AgentRead]:
        """List the current (highest-revision) snapshot of every agent."""
        return [
            agent_to_read(profile)
            for profile in repository.list_current(include_archived=include_archived)
        ]

    @app.get("/agents/{agent_id}", response_model=AgentRead)
    def get_agent(
        agent_id: str,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Return the current snapshot of ``agent_id`` or ``404``."""
        profile = repository.get_current(agent_id)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent profile not found",
            )
        return agent_to_read(profile)

    @app.get(
        "/agents/{agent_id}/revisions",
        response_model=list[AgentRead],
    )
    def list_agent_revisions(
        agent_id: str,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> list[AgentRead]:
        """Return every immutable snapshot of ``agent_id`` by revision.

        An unknown agent yields an empty list, matching the repository
        contract for listing history.
        """
        return [
            agent_to_read(profile)
            for profile in repository.list_history(agent_id)
        ]

    @app.get(
        "/agents/{agent_id}/revisions/{revision}",
        response_model=AgentRead,
    )
    def get_agent_revision(
        agent_id: str,
        revision: int,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Return one immutable snapshot or ``404`` when it is absent."""
        profile = repository.get_revision(agent_id, revision)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent profile revision not found",
            )
        return agent_to_read(profile)

    @app.patch("/agents/{agent_id}", response_model=AgentRead)
    def patch_agent(
        agent_id: str,
        payload: AgentPatch,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Append a new revision after an optimistic revision check.

        Omitted fields are copied from the current snapshot; supplied fields
        replace the stored value whole. ``409 Conflict`` reports the current
        revision when known and leaves all snapshots unchanged; ``404`` means
        the agent has no snapshots.
        """
        try:
            profile = repository.update(
                agent_id=agent_id,
                expected_revision=payload.expected_revision,
                **payload.replacements(),
            )
        except AgentProfileNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"agent profile not found: {error.agent_id}",
            ) from error
        except AgentProfileRevisionConflictError as error:
            detail = (
                "agent profile revision conflict: expected revision "
                f"{error.expected_revision}"
            )
            if error.actual_revision is not None:
                detail += f", current revision {error.actual_revision}"
            else:
                detail += ", current revision unknown"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            ) from error
        return agent_to_read(profile)

    @app.delete("/agents/{agent_id}", response_model=AgentRead)
    def delete_agent(
        agent_id: str,
        request: Request,
        repository: AgentProfileRepository = Depends(get_agent_repository),
    ) -> AgentRead:
        """Archive (soft-delete) an agent profile."""
        if agent_id in CORE_SYSTEM_AGENTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Core system agent '{agent_id}' cannot be archived",
            )
        current = repository.get_current(agent_id)
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent profile not found",
            )
        try:
            profile = repository.archive(agent_id, expected_revision=current.revision)
            try:
                from agent_manager.knowledge import KnowledgeBase
                kb = getattr(request.app.state, "knowledge_base", None)
                if kb is None:
                    kb = KnowledgeBase()
                target = kb.agents_dir / f"{agent_id}.md"
                if target.is_file():
                    target.unlink()
                    kb.git_commit(f"Archive agent profile: {agent_id}")
            except Exception as e:
                logger.warning("Could not remove agent markdown file on delete: %s", e)
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        return agent_to_read(profile)

    return app

