"""SQLite-backed storage boundary for the Kuzya.

Public interfaces:
    kuzya.storage.Database                    -- engine/session owner for one SQLite file.
    kuzya.storage.TaskRepository              -- durable create/get/list/import task operations.
    kuzya.storage.Task                        -- SQLAlchemy ORM row for the ``tasks`` table.
    kuzya.storage.TaskImportResult            -- created/skipped task import counts.
    kuzya.storage.DuplicateTaskIdError        -- duplicate caller-supplied task id.
    kuzya.storage.TaskAlreadyRunningError     -- attempt to restart an actively running task.
    kuzya.storage.ConversationRepository      -- create/get/list conversations and append/list messages.
    kuzya.storage.Conversation                -- SQLAlchemy ORM row for the ``conversations`` table.
    kuzya.storage.Message                     -- SQLAlchemy ORM row for the ``messages`` table.
    kuzya.storage.MessageAppendResult         -- inserted/replayed outcome of one append.
    kuzya.storage.ConversationNotFoundError   -- append to an absent conversation.
    kuzya.storage.DuplicateConversationIdError -- duplicate caller-supplied conversation id.
    kuzya.storage.MessageConflictError        -- idempotency key reused with a different payload.
    kuzya.storage.Turn                        -- SQLAlchemy ORM row for the ``turns`` table.
    kuzya.storage.Base                        -- declarative metadata root used by Alembic.
    kuzya.storage.AgentProfile                -- SQLAlchemy ORM row for the ``agent_profiles`` table.
    kuzya.storage.AgentProfileRepository      -- immutable versioned agent-profile snapshots.
    kuzya.storage.AgentProfileNotFoundError   -- update/list target with no snapshots.
    kuzya.storage.DuplicateAgentProfileError  -- create for an agent_id that already exists.
    kuzya.storage.AgentProfileRevisionConflictError -- update with a stale expected revision.
    kuzya.storage.ConversationRuntime              -- SQLAlchemy ORM row for the ``conversation_runtimes`` table.
    kuzya.storage.ConversationRuntimeRepository    -- per-conversation secretary session binding and model-call reservation.
    kuzya.storage.RuntimeReservation                -- reserved/cached outcome of one reservation attempt.
    kuzya.storage.ConversationRuntimeBusyError      -- a reservation is already active.
    kuzya.storage.ConversationRuntimeBlockedError   -- the binding is blocked and fails closed.
    kuzya.storage.ConversationRuntimeOwnershipError -- stale or invalid reservation ownership.
    kuzya.storage.ConversationRuntimeReferenceError -- missing conversation, profile or turn reference.
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
from .analysis import (
    AnalysisError,
    AnalysisRepository,
    AnalysisReportNotFoundError,
    RecommendationNotFoundError,
    RecommendationStateError,
)
from .database import DEFAULT_BUSY_TIMEOUT_MS, Database
from .models import (
    RECOMMENDATION_STATUSES,
    RECOMMENDATION_STATUS_APPLIED,
    RECOMMENDATION_STATUS_APPROVED,
    RECOMMENDATION_STATUS_CONFLICT,
    RECOMMENDATION_STATUS_FAILED,
    RECOMMENDATION_STATUS_PROPOSED,
    AgentProfile,
    AnalysisReport,
    Base,
    Conversation,
    Message,
    Operation,
    Recommendation,
    Task,
    Turn,
)
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
    TaskAlreadyRunningError,
    TaskImportResult,
    TaskNotFoundError,
    TaskRepository,
)

__all__ = [
    "AgentProfile",
    "AgentProfileNotFoundError",
    "AgentProfileRepository",
    "AgentProfileRevisionConflictError",
    "AnalysisError",
    "AnalysisReport",
    "AnalysisReportNotFoundError",
    "AnalysisRepository",
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
    "RECOMMENDATION_STATUSES",
    "RECOMMENDATION_STATUS_APPLIED",
    "RECOMMENDATION_STATUS_APPROVED",
    "RECOMMENDATION_STATUS_CONFLICT",
    "RECOMMENDATION_STATUS_FAILED",
    "RECOMMENDATION_STATUS_PROPOSED",
    "Recommendation",
    "RecommendationNotFoundError",
    "RecommendationStateError",
    "RuntimeReservation",
    "Task",
    "TaskAlreadyRunningError",
    "TaskImportResult",
    "TaskNotFoundError",
    "TaskRepository",
    "Turn",
    "clear_interrupted_block",
    "interrupted_blocked_reason",
    "is_interrupted_block",
    "release_interrupted_reservation",
]
