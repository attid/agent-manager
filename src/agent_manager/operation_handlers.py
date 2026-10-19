"""Concrete executors for the built-in Git operation kinds.

The Git executors are the durable replacement for the previous synchronous
``actions.json`` handling. They read the task, resolve its workspace, invoke
:class:`~agent_manager.git_engine.GitEngine` and update the task status only
after the Git call succeeds. A Git failure raises, so the operation is recorded
``failed`` instead of reporting success.

Review (T03) and analysis (T04) executors are deliberately not registered here;
until those tasks land such operations fail visibly with an unknown-executor
diagnostic rather than pretending to succeed.
"""

from __future__ import annotations

import logging
from typing import Any

from .git_engine import GitEngine
from .operation_worker import (
    OPERATION_KIND_GIT_COMMIT_PUSH,
    OPERATION_KIND_GIT_ROLLBACK,
    OperationContext,
    OperationExecutionError,
    OperationWorker,
)
from .storage import TaskRepository

logger = logging.getLogger(__name__)


def _load_task_context(
    context: OperationContext,
    action: str,
) -> tuple[TaskRepository, str, Any, str, list[str]]:
    """Resolve the target task, workspace and metadata for a Git action."""
    database = context.instance.database
    task_id = (context.target_id or "").strip()
    if not task_id:
        raise OperationExecutionError(f"{action} requires a target task id")
    repository = TaskRepository(database)
    task = repository.get(task_id)
    if task is None:
        raise OperationExecutionError(f"task not found: {task_id}")
    workspace = task.workspace
    if not workspace and context.instance.default_workspace is not None:
        workspace = str(context.instance.default_workspace)
    if not workspace:
        raise OperationExecutionError(
            f"{action} has no workspace for task {task_id}"
        )
    meta = dict(task.meta or {})
    changed_files = [
        str(item) for item in (meta.get("changed_files") or [])
    ]
    return repository, task_id, task, workspace, changed_files


def git_commit_push_handler(
    context: OperationContext,
) -> dict[str, Any]:
    """Commit (and optionally push) the task's changes, then mark it done."""
    repository, task_id, task, workspace, changed_files = _load_task_context(
        context, "git.commit_push"
    )
    meta = dict(task.meta or {})
    custom_message = str(context.input.get("message") or "").strip() or None
    push = bool(context.input.get("push", True))
    engine = GitEngine(default_workspace=workspace)
    common: dict[str, Any] = {
        "workspace": workspace,
        "task_id": task_id,
        "title": task.title,
        "summary": meta.get("summary"),
        "changed_files": changed_files,
        "custom_message": custom_message,
    }
    if push:
        result = engine.commit_and_push(**common)
    else:
        result = engine.commit_local(**common)
    if not result.success:
        raise OperationExecutionError(
            result.message or "git commit/push failed"
        )
    repository.update(task_id, status="done")
    return {
        "action": result.action,
        "message": result.message,
        "commit_hash": result.commit_hash,
        "branch": result.branch,
        "ci_status": result.ci_status,
        "pushed": push,
        "task_status": "done",
    }


def git_rollback_handler(context: OperationContext) -> dict[str, Any]:
    """Discard the task's working-tree changes and cancel the task."""
    repository, task_id, _task, workspace, changed_files = _load_task_context(
        context, "git.rollback"
    )
    engine = GitEngine(default_workspace=workspace)
    result = engine.rollback(
        workspace=workspace, changed_files=changed_files
    )
    if not result.success:
        raise OperationExecutionError(result.message or "git rollback failed")
    repository.update(task_id, status="cancelled")
    return {
        "action": result.action,
        "message": result.message,
        "task_status": "cancelled",
    }


def register_default_operation_handlers(worker: OperationWorker) -> None:
    """Register the built-in Git executors on ``worker``.

    Called from the runner composition root. Review and analysis are left
    unregistered on purpose so T03/T04 can attach them at the same seam.
    """
    worker.register(OPERATION_KIND_GIT_COMMIT_PUSH, git_commit_push_handler)
    worker.register(OPERATION_KIND_GIT_ROLLBACK, git_rollback_handler)


__all__ = [
    "git_commit_push_handler",
    "git_rollback_handler",
    "register_default_operation_handlers",
]
