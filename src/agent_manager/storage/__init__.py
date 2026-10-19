"""SQLite-backed storage boundary for the Agent Manager.

Public interfaces:
    agent_manager.storage.Database                    -- engine/session owner for one SQLite file.
    agent_manager.storage.TaskRepository              -- durable create/get/list/import task operations.
    agent_manager.storage.Task                        -- SQLAlchemy ORM row for the ``tasks`` table.
    agent_manager.storage.TaskImportResult            -- created/skipped task import counts.
    agent_manager.storage.DuplicateTaskIdError        -- duplicate caller-supplied task id.
    agent_manager.storage.ConversationRepository      -- create/get/list conversations and append/list messages.
    agent_manager.storage.Conversation                -- SQLAlchemy ORM row for the ``conversations`` table.
    agent_manager.storage.Message                     -- SQLAlchemy ORM row for the ``messages`` table.
    agent_manager.storage.MessageAppendResult         -- inserted/replayed outcome of one append.
    agent_manager.storage.ConversationNotFoundError   -- append to an absent conversation.
    agent_manager.storage.DuplicateConversationIdError -- duplicate caller-supplied conversation id.
    agent_manager.storage.MessageConflictError        -- idempotency key reused with a different payload.
    agent_manager.storage.Turn                        -- SQLAlchemy ORM row for the ``turns`` table.
    agent_manager.storage.Base                        -- declarative metadata root used by Alembic.
    agent_manager.storage.AgentProfile                -- SQLAlchemy ORM row for the ``agent_profiles`` table.
    agent_manager.storage.AgentProfileRepository      -- immutable versioned agent-profile snapshots.
    agent_manager.storage.AgentProfileNotFoundError   -- update/list target with no snapshots.
    agent_manager.storage.DuplicateAgentProfileError  -- create for an agent_id that already exists.
    agent_manager.storage.AgentProfileRevisionConflictError -- update with a stale expected revision.
    agent_manager.storage.ConversationRuntime              -- SQLAlchemy ORM row for the ``conversation_runtimes`` table.
    agent_manager.storage.ConversationRuntimeRepository    -- per-conversation secretary session binding and model-call reservation.
    agent_manager.storage.RuntimeReservation                -- reserved/cached outcome of one reservation attempt.
    agent_manager.storage.ConversationRuntimeBusyError      -- a reservation is already active.
    agent_manager.storage.ConversationRuntimeBlockedError   -- the binding is blocked and fails closed.
    agent_manager.storage.ConversationRuntimeOwnershipError -- stale or invalid reservation ownership.
    agent_manager.storage.ConversationRuntimeReferenceError -- missing conversation, profile or turn reference.
"""

from .agents import (
    CORE_SYSTEM_AGENTS,
    AgentProfileNotFoundError,
    AgentProfileRepository,
    AgentProfileRevisionConflictError,
    DuplicateAgentProfileError,
)
from .conversation_runtime import (
    INTERRUPTED_BLOCKED_PREFIX,
    ConversationRuntime,
    ConversationRuntimeAgentConflictError,
    ConversationRuntimeAttachmentError,
    ConversationRuntimeBlockedError,
    ConversationRuntimeBusyError,
    ConversationRuntimeError,
    ConversationRuntimeInvalidError,
    ConversationRuntimeNotFoundError,
    ConversationRuntimeOwnershipError,
    ConversationRuntimeReferenceError,
    ConversationRuntimeRepository,
    ConversationRuntimeSessionConflictError,
    ConversationRuntimeTurnStateError,
    RuntimeReservation,
    clear_interrupted_block,
    interrupted_blocked_reason,
    is_interrupted_block,
    release_interrupted_reservation,
)
from .database import DEFAULT_BUSY_TIMEOUT_MS, Database
from .models import AgentProfile, Base, Conversation, Message, Operation, Task, Turn
from .operations import (
    OPERATION_QUEUED_STATUSES,
    OPERATION_STATUSES,
    OPERATION_STATUS_ACCEPTED,
    OPERATION_STATUS_FAILED,
    OPERATION_STATUS_INTERRUPTED,
    OPERATION_STATUS_PENDING,
    OPERATION_STATUS_RUNNING,
    OPERATION_STATUS_SUCCEEDED,
    OPERATION_TERMINAL_STATUSES,
    OperationConflictError,
    OperationError,
    OperationNotFoundError,
    OperationRepository,
    OperationStaleClaimError,
    OperationSubmitResult,
)
from .repository import (
    ConversationNotFoundError,
    ConversationRepository,
    DuplicateConversationIdError,
    DuplicateTaskIdError,
    MessageAppendResult,
    MessageConflictError,
    TaskImportResult,
    TaskNotFoundError,
    TaskRepository,
)

__all__ = [
    "AgentProfile",
    "AgentProfileNotFoundError",
    "AgentProfileRepository",
    "AgentProfileRevisionConflictError",
    "Base",
    "Conversation",
    "ConversationNotFoundError",
    "ConversationRepository",
    "ConversationRuntime",
    "ConversationRuntimeAgentConflictError",
    "ConversationRuntimeAttachmentError",
    "ConversationRuntimeBlockedError",
    "ConversationRuntimeBusyError",
    "ConversationRuntimeError",
    "ConversationRuntimeInvalidError",
    "ConversationRuntimeNotFoundError",
    "ConversationRuntimeOwnershipError",
    "ConversationRuntimeReferenceError",
    "ConversationRuntimeRepository",
    "ConversationRuntimeSessionConflictError",
    "ConversationRuntimeTurnStateError",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "Database",
    "DuplicateAgentProfileError",
    "DuplicateConversationIdError",
    "DuplicateTaskIdError",
    "INTERRUPTED_BLOCKED_PREFIX",
    "Message",
    "MessageAppendResult",
    "MessageConflictError",
    "OPERATION_QUEUED_STATUSES",
    "OPERATION_STATUSES",
    "OPERATION_STATUS_ACCEPTED",
    "OPERATION_STATUS_FAILED",
    "OPERATION_STATUS_INTERRUPTED",
    "OPERATION_STATUS_PENDING",
    "OPERATION_STATUS_RUNNING",
    "OPERATION_STATUS_SUCCEEDED",
    "OPERATION_TERMINAL_STATUSES",
    "Operation",
    "OperationConflictError",
    "OperationError",
    "OperationNotFoundError",
    "OperationRepository",
    "OperationStaleClaimError",
    "OperationSubmitResult",
    "RuntimeReservation",
    "Task",
    "TaskImportResult",
    "TaskNotFoundError",
    "TaskRepository",
    "Turn",
    "clear_interrupted_block",
    "interrupted_blocked_reason",
    "is_interrupted_block",
    "release_interrupted_reservation",
]
