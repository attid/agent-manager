"""Background task execution worker for running developer agents on in_progress tasks."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from .capabilities import write_handbook_discovery
from .instance import InstanceContext
from .logging import bind_log_context
from .permissions import (
    ExecutionPermissions,
    actions_for_tools,
    resources_for_execution,
)
from .workspaces import resolve_workspace
from .runtime_defaults import RuntimeDefaultsResolver
from .runner_output import extract_runner_result
from .profile_runtime import _build_role_config
from .review_executor import (
    VERDICT_APPROVED,
    ReviewExecutionError,
    ReviewExecutor,
    ReviewRequest,
)
from .storage import (
    AgentProfileRepository,
    Database,
    Task,
    TaskRepository,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class TaskOutcome:
    """Outcome of one task execution."""

    task_id: str
    status: str  # "completed" or "failed"
    summary: str | None = None
    changed_files: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0.0
    worker_id: str | None = None


class TaskExecutionWorker:
    """Continuously claims in_progress tasks and executes them via pi_runner."""

    def __init__(
        self,
        database: Database,
        artifact_root: str | os.PathLike[str],
        *,
        agent_id: str = "agent-manager-developer",
        runner_argv_prefix: Sequence[str] = (sys.executable, "-m", "pi_runner"),
        workspace_map: Mapping[str, str] | None = None,
        default_workspace: str | os.PathLike[str] | None = None,
        poll_interval_seconds: float = 1.0,
        worker_id: str | None = None,
        env: Mapping[str, str] | None = None,
        on_task_finished: Callable[[Task, TaskOutcome], None] | None = None,
        knowledge_base: Any | None = None,
        runtime_defaults: RuntimeDefaultsResolver | None = None,
        instance: InstanceContext | None = None,
        permissions: ExecutionPermissions | None = None,
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.agent_id = agent_id
        self.runner_argv_prefix = list(runner_argv_prefix)
        self.workspace_map = dict(workspace_map or {})
        self.default_workspace = (
            Path(default_workspace).expanduser().resolve() if default_workspace else None
        )
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.worker_id = worker_id or f"task-worker-{uuid.uuid4().hex[:8]}"
        self.env = dict(env) if env is not None else None
        self.on_task_finished = on_task_finished
        self.knowledge_base = knowledge_base
        self.runtime_defaults = runtime_defaults or RuntimeDefaultsResolver()
        self.instance = instance
        self.permissions = permissions

        self._repository = TaskRepository(database)
        self._agent_repository = AgentProfileRepository(database)

        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

        logger.info(
            "TaskExecutionWorker initialized: worker_id=%s, agent_id=%s, poll_interval=%.1fs, artifact_root=%s, default_workspace=%s",
            self.worker_id,
            self.agent_id,
            self.poll_interval_seconds,
            self.artifact_root,
            self.default_workspace,
        )

    def _resolve_workspace(self, workspace_name: str | None) -> Path:
        return resolve_workspace(workspace_name, default_workspace=self.default_workspace, workspace_map=self.workspace_map)

    def step(self) -> TaskOutcome | None:
        """Claim and execute at most one in_progress task.

        Returns None when no task was available. Catches all exceptions during
        execution and marks the task failed with diagnostic error without crashing.
        """
        task = self._repository.claim_in_progress(worker_id=self.worker_id)
        if task is None:
            return None

        started = time.perf_counter()
        ws_name = task.workspace or "default"
        with bind_log_context(
            worker_id=self.worker_id,
            task_id=task.id,
        ):
            logger.info(
                "Task claimed for execution: task_id=%s title=%r workspace=%s",
                task.id,
                task.title,
                ws_name,
                extra={"workspace": ws_name, "event": "task_claim"},
            )
            execution_grant = None
            try:
                selected_agent_id = task.agent_id if task.agent_id is not None else self.agent_id
                # Resolve the selected profile before choosing its default workspace.
                if self.knowledge_base is not None and hasattr(self.knowledge_base, "sync_agent_from_file"):
                    self.knowledge_base.sync_agent_from_file(selected_agent_id, self.database)
                profile = self._agent_repository.get_current(selected_agent_id)
                if profile is None:
                    raise RuntimeError(f"Agent profile not found in registry: {selected_agent_id}")
                if profile.runner_config.get("archived") or profile.role == "archived":
                    raise RuntimeError(f"Agent is archived: {selected_agent_id}")
                requested_workspace = task.workspace if task.workspace is not None else profile.runner_config.get("project")
                project_dir = self._resolve_workspace(requested_workspace)
                logger.info(
                    "Task %s workspace resolved to project_dir=%s",
                    task.id,
                    project_dir,
                    extra={"workspace": ws_name},
                )

                # 2. Prepare task directory and prompt
                exec_dir = (
                    self.artifact_root / f"task_{task.id}_{uuid.uuid4().hex[:8]}"
                )
                exec_dir.mkdir(parents=True, exist_ok=True)
                task_file = exec_dir / "task.md"
                task_text = (
                    f"# Task {task.id}: {task.title}\n\n"
                    f"{task.description or ''}\n"
                )
                task_file.write_text(task_text, encoding="utf-8")

                # 3. Prepare the selected execution configuration.
                meta_data = dict(task.meta or {})
                provenance_data = dict(meta_data.get("provenance") or {})
                resume_session_id = (
                    meta_data.get("runner_session_id")
                    or provenance_data.get("runner_session_id")
                    or provenance_data.get("session_id")
                )
                resume_state_dir = (
                    meta_data.get("runner_state_dir")
                    or provenance_data.get("runner_state_dir")
                )

                previous_context = meta_data.get("execution_context") or {}
                previous_agent = previous_context.get("agent_id") or provenance_data.get("agent_id")
                previous_project = previous_context.get("host_path") or provenance_data.get("project")
                if (previous_agent is not None and previous_agent != selected_agent_id) or (previous_project is not None and previous_project != str(project_dir)):
                    # A new executor or workspace must never inherit another native session.
                    # Earlier state and artifacts are retained in their original directory.
                    resume_session_id = None
                    resume_state_dir = None
                runner_config = dict(profile.runner_config or {})
                runner_config["project"] = str(project_dir)

                # Extract skills from profile
                skills = list(runner_config.get("skills") or [])
                if not skills and hasattr(profile, "skills") and profile.skills:
                    for sk in profile.skills:
                        if isinstance(sk, dict) and "skill" in sk:
                            skills.append(str(sk["skill"]))
                        elif isinstance(sk, str):
                            skills.append(sk)
                if skills:
                    runner_config["skills"] = skills

                # Allow git commit for devops tasks or explicit flag
                if meta_data.get("allow_git_commit") or meta_data.get("task_type") == "devops":
                    runner_config["allow_git_commit"] = True

                state_dir = (
                    Path(resume_state_dir)
                    if resume_state_dir and Path(resume_state_dir).is_dir()
                    else exec_dir / "state"
                )
                state_dir.mkdir(parents=True, exist_ok=True)
                runner_config["state_dir"] = str(state_dir)

                outbox_dir = exec_dir / "outbox"
                outbox_dir.mkdir(parents=True, exist_ok=True)
                runner_config["outbox_dir"] = str(outbox_dir)

                attachments = meta_data.get("attachments") or []
                if attachments:
                    inbox_dir = exec_dir / "inbox"
                    inbox_dir.mkdir(parents=True, exist_ok=True)
                    for att in attachments:
                        if isinstance(att, dict):
                            att_name = att.get("name") or att.get("artifact_name")
                            source_task_id = att.get("task_id")
                            source_path = att.get("path")
                        else:
                            att_name = getattr(att, "name", None) or getattr(att, "artifact_name", None)
                            source_task_id = getattr(att, "task_id", None)
                            source_path = getattr(att, "path", None)

                        resolved_path: Path | None = None
                        if source_path:
                            sp = Path(source_path)
                            if sp.is_file():
                                resolved_path = sp

                        if resolved_path is None:
                            effective_task_id = source_task_id or task.id
                            name_for_candidate = att_name or (Path(source_path).name if source_path else None)
                            if effective_task_id and name_for_candidate:
                                candidate = (
                                    self.artifact_root
                                    / str(effective_task_id)
                                    / "artifacts"
                                    / name_for_candidate
                                )
                                if candidate.is_file():
                                    resolved_path = candidate

                        if resolved_path is not None:
                            raw_target_name = att_name or resolved_path.name
                            safe_name = Path(raw_target_name).name
                            if not safe_name or safe_name in {".", ".."}:
                                safe_name = resolved_path.name
                            dest_file = inbox_dir / safe_name
                            try:
                                if dest_file.exists():
                                    try:
                                        dest_file.unlink()
                                    except OSError:
                                        dest_file.chmod(0o644)
                                shutil.copy2(resolved_path, dest_file)
                                dest_file.chmod(0o444)
                                logger.info(
                                    "Staged attachment %s to %s (chmod 0o444) for task %s",
                                    resolved_path,
                                    dest_file,
                                    task.id,
                                )
                            except Exception as err:
                                logger.warning(
                                    "Failed to stage attachment %s to %s for task %s: %s",
                                    resolved_path,
                                    dest_file,
                                    task.id,
                                    err,
                                )
                        else:
                            logger.warning(
                                "Attachment source file not found for task %s: att=%s, path=%s, candidate_task_id=%s, candidate_name=%s",
                                task.id,
                                att,
                                source_path,
                                source_task_id or task.id,
                                att_name,
                            )
                    runner_config["inbox_dir"] = str(inbox_dir)

                instructions = list(runner_config.get("instructions") or [])

                # Inject project summary if present in knowledge base
                if self.knowledge_base is not None:
                    summary_candidates = []
                    if task.workspace:
                        summary_candidates.append(
                            self.knowledge_base.projects_dir / task.workspace / "summary.md"
                        )
                    summary_candidates.append(
                        self.knowledge_base.projects_dir / project_dir.name / "summary.md"
                    )
                    for s_cand in summary_candidates:
                        if s_cand.is_file():
                            instructions.append(str(s_cand))
                            logger.info("Injected project summary from %s into task %s", s_cand, task.id)
                            break

                prompt_file = exec_dir / "system_prompt.md"
                prompt_file.write_text(profile.system_prompt, encoding="utf-8")
                discovery_path = write_handbook_discovery(
                    exec_dir,
                    agent_id=selected_agent_id,
                    tools=list(runner_config.get("tools") or []),
                )
                if discovery_path is not None:
                    instructions.append(str(discovery_path))
                # Propagate allowed_hosts from runner_config and task metadata
                allowed_hosts = list(runner_config.get("allowed_hosts") or [])
                task_meta = task.meta or {}
                if task_meta.get("allowed_hosts") and isinstance(task_meta.get("allowed_hosts"), list):
                    for host in task_meta["allowed_hosts"]:
                        if isinstance(host, str) and host not in allowed_hosts:
                            allowed_hosts.append(host)
                if allowed_hosts:
                    runner_config["allowed_hosts"] = allowed_hosts

                workspace_file = exec_dir / "workspace.md"
                workspace_file.write_text(
                    f"# Execution workspace\nHost directory: {project_dir}\n"
                    f"Sandbox root: /workspace\n"
                    f"Working directory: /workspace/{runner_config.get('workdir', '.')}\n"
                    f"Project read-only: {bool(runner_config.get('project_read_only', False))}\n"
                    f"Sandbox network: {runner_config.get('network', True)}\n"
                    f"Allowed hosts: {runner_config.get('allowed_hosts', [])}\n"
                    "Outbox: /outbox (writable artifacts; write output files, logs or reports here)\n"
                    "Inbox: /inbox (read-only input attachments)\n"
                    "Use the sandbox paths above. A directory named Project is not automatically added below /workspace.\n",
                    encoding="utf-8",
                )
                runner_config["instructions"] = instructions
                runner_config = _build_role_config(runner_config, agent_id=selected_agent_id, prompt_path=prompt_file)
                runner_config["instructions"].append(str(workspace_file))
                runtime_selection = self.runtime_defaults.resolve(runner_config)
                runner_config = runtime_selection.role_config
                execution_context = {
                    "agent_id": selected_agent_id,
                    "profile_revision": profile.revision,
                    "host_path": str(project_dir),
                    "sandbox_path": "/workspace",
                    "project_read_only": bool(runner_config.get("project_read_only", False)),
                    "network": runner_config.get("network", True),
                    "allowed_hosts": list(runner_config.get("allowed_hosts") or []),
                    "provider": runtime_selection.provider,
                    "model": runtime_selection.model,
                }
                self._repository.update(task.id, metadata={**(task.meta or {}), "execution_context": execution_context})

                # Ensure knowledge directory is in readonly_paths if available
                if (
                    self.knowledge_base is not None
                    and self.knowledge_base.root_dir.is_dir()
                ):
                    ro_paths = list(runner_config.get("readonly_paths") or [])
                    kb_root_str = str(self.knowledge_base.root_dir)
                    if kb_root_str not in ro_paths:
                        ro_paths.append(kb_root_str)
                    runner_config["readonly_paths"] = ro_paths

                # Explicit per-instance mounts and execution-scoped backend
                # credentials. Flagship/admin tokens never reach the child.
                if self.instance is not None:
                    if self.instance.knowledge_root.is_dir():
                        runner_config["knowledge_dir"] = str(
                            self.instance.knowledge_root
                        )
                    if self.instance.skills_root.is_dir():
                        runner_config["skills_dir"] = str(
                            self.instance.skills_root
                        )
                    if self.permissions is not None and self.instance.backend_url:
                        exec_actions = actions_for_tools(
                            runner_config.get("tools") or []
                        )
                        resources = resources_for_execution(
                            exec_actions,
                            task_id=task.id,
                            agent_id=selected_agent_id,
                        )
                        execution_grant = self.permissions.issue(
                            agent_id=selected_agent_id,
                            actions=exec_actions,
                            resources=resources,
                            label=f"task:{task.id}",
                        )
                        runner_config["backend"] = self.instance.child_backend(
                            execution_grant.token
                        )

                role_file = exec_dir / "role.json"
                role_file.write_text(
                    json.dumps(runner_config, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.debug(
                    "Prepared execution files in exec_dir=%s (role=%s, task=%s)",
                    exec_dir,
                    role_file,
                    task_file,
                )

                # 4. Invoke child runner (resume if previous session exists)
                if (
                    resume_session_id
                    and state_dir.is_dir()
                    and (state_dir / "sessions" / resume_session_id).is_dir()
                ):
                    cmd = [
                        *self.runner_argv_prefix,
                        "resume",
                        "--state-dir",
                        str(state_dir),
                        "--session",
                        resume_session_id,
                        "--agent",
                        str(role_file),
                        "--task",
                        str(task_file),
                    ]
                    logger.info("Resuming existing runner session %s for task %s", resume_session_id, task.id)
                else:
                    cmd = [
                        *self.runner_argv_prefix,
                        "run",
                        "--agent",
                        str(role_file),
                        "--task",
                        str(task_file),
                    ]

                child_env = dict(self.env or os.environ)
                logger.info(
                    "Invoking runner command for task %s: cmd=%s cwd=%s",
                    task.id,
                    cmd,
                    project_dir,
                )
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(project_dir),
                    env=child_env,
                    check=False,
                )

                duration_ms = (time.perf_counter() - started) * 1000.0
                logger.info(
                    "Runner finished for task %s: returncode=%d duration_ms=%.1f stdout_len=%d stderr_len=%d",
                    task.id,
                    result.returncode,
                    duration_ms,
                    len(result.stdout),
                    len(result.stderr),
                )
                if result.stderr:
                    logger.warning(
                        "Runner stderr for task %s:\n%s",
                        task.id,
                        result.stderr.strip()[:2000],
                    )

                if result.returncode != 0:
                    err_msg = (
                        f"Runner process exited with code {result.returncode}.\n"
                        f"Stderr: {result.stderr[:2000]}\n"
                        f"Stdout: {result.stdout[:1000]}"
                    )
                    raise RuntimeError(err_msg)

                stdout_clean = result.stdout.strip()
                runner_result = extract_runner_result(result.stdout)
                if runner_result is None:
                    raise RuntimeError(
                        "Failed to parse runner JSON output: no JSON object "
                        f"found. Stdout: {stdout_clean[:1000]!r}"
                    )

                status = runner_result.get("status")
                if status not in ("completed", "waiting_for_input"):
                    raise RuntimeError(
                        f"Runner reported status {status!r}: {runner_result.get('errors_path')}"
                    )

                summary_path = runner_result.get("summary_path")
                summary_text = (
                    Path(summary_path).read_text(encoding="utf-8")
                    if summary_path and Path(summary_path).exists()
                    else ("Требуется уточнение вопроса." if status == "waiting_for_input" else "Задача выполнена.")
                )
                changed_files = list(runner_result.get("changed_files") or [])

                # Store session details in runner_result for provenance
                session_id = runner_result.get("session_id")
                if session_id:
                    runner_result["runner_session_id"] = session_id
                    runner_result["runner_state_dir"] = str(state_dir)

                requires_review = bool(
                    meta_data.get("requires_review")
                    or (meta_data.get("task_type") == "coding" and meta_data.get("requires_review") is not False)
                )
                if status == "completed" and requires_review and changed_files:
                    review_outcome = self._run_review_cycle(
                        task=task,
                        project_dir=project_dir,
                        changed_files=changed_files,
                        developer_summary=summary_text,
                        exec_dir=exec_dir,
                        role_file=role_file,
                        state_dir=state_dir,
                        child_env=child_env,
                        session_id=session_id,
                    )
                    if review_outcome.get("needs_user_input"):
                        status = "waiting_for_input"
                        summary_text = review_outcome.get("summary", summary_text)
                        runner_result["clarification"] = review_outcome.get("clarification")
                    else:
                        summary_text = review_outcome.get("summary", summary_text)
                        changed_files = review_outcome.get("changed_files", changed_files)

                if status == "waiting_for_input":
                    task_status = "waiting_for_input"
                    outcome_status = "waiting_for_input"
                elif (requires_review and changed_files) or (meta_data.get("task_type") == "coding" and changed_files):
                    task_status = "ready_to_commit"
                    outcome_status = "ready_to_commit"
                else:
                    task_status = "done"
                    outcome_status = "completed"
                task_artifacts_dir = self.artifact_root / str(task.id) / "artifacts"
                task_artifacts_dir.mkdir(parents=True, exist_ok=True)
                collected_artifacts = []
                if outbox_dir.is_dir():
                    for item in sorted(outbox_dir.iterdir()):
                        if item.is_file():
                            dest = task_artifacts_dir / item.name
                            shutil.copy2(item, dest)
                            collected_artifacts.append({
                                "name": item.name,
                                "path": str(dest),
                                "size": dest.stat().st_size,
                            })
                if summary_path and Path(summary_path).is_file():
                    sum_dest = task_artifacts_dir / "summary.md"
                    shutil.copy2(summary_path, sum_dest)
                    collected_artifacts.append({
                        "name": "summary.md",
                        "path": str(sum_dest),
                        "size": sum_dest.stat().st_size,
                    })
                if runner_result.get("session_path") and Path(runner_result["session_path"]).is_file():
                    sess_path = Path(runner_result["session_path"])
                    sess_dest = task_artifacts_dir / "session.jsonl"
                    shutil.copy2(sess_path, sess_dest)
                    collected_artifacts.append({
                        "name": "session.jsonl",
                        "path": str(sess_dest),
                        "size": sess_dest.stat().st_size,
                    })
                runner_result["artifacts"] = collected_artifacts

                updated_task = self._repository.complete_execution(
                    task.id,
                    status=task_status,
                    summary=summary_text,
                    changed_files=changed_files,
                    provenance=runner_result,
                )
                outcome = TaskOutcome(
                    task_id=task.id,
                    status=outcome_status,
                    summary=summary_text,
                    changed_files=changed_files,
                    duration_ms=duration_ms,
                    worker_id=self.worker_id,
                )
                logger.info(
                    "Task %s execution finished with status=%s duration_ms=%.2f changed_files=%s",
                    task.id,
                    outcome_status,
                    duration_ms,
                    changed_files,
                    extra={"event": "task_finished", "workspace": ws_name, "outcome": outcome_status},
                )
                if self.on_task_finished:
                    try:
                        self.on_task_finished(updated_task, outcome)
                    except Exception as cb_err:
                        logger.error(
                            "on_task_finished callback raised for task %s: %s",
                            task.id,
                            cb_err,
                            exc_info=cb_err,
                        )
                return outcome

            except Exception as error:
                duration_ms = (time.perf_counter() - started) * 1000.0
                err_str = f"{type(error).__name__}: {error}"
                logger.error(
                    "Task %s execution failed: %s",
                    task.id,
                    err_str,
                    exc_info=error,
                    extra={"event": "task_failed", "workspace": ws_name},
                )
                try:
                    updated_task = self._repository.complete_execution(
                        task.id,
                        status="failed",
                        error=err_str,
                    )
                except Exception as save_err:
                    logger.critical(
                        "Failed to update task %s to failed status in DB: %s",
                        task.id,
                        save_err,
                        exc_info=save_err,
                    )
                    updated_task = task

                outcome = TaskOutcome(
                    task_id=task.id,
                    status="failed",
                    error=err_str,
                    duration_ms=duration_ms,
                    worker_id=self.worker_id,
                )
                if self.on_task_finished:
                    try:
                        self.on_task_finished(updated_task, outcome)
                    except Exception as cb_err:
                        logger.error(
                            "on_task_finished callback raised for failed task %s: %s",
                            task.id,
                            cb_err,
                            exc_info=cb_err,
                        )
                return outcome
            finally:
                # The execution-scoped credential lives exactly as long as
                # the child execution. Revoking it must not affect the
                # service's stable authentication mode.
                if execution_grant is not None and self.permissions is not None:
                    self.permissions.revoke(execution_grant.token)

    def _run_review_cycle(
        self,
        task: Task,
        project_dir: Path,
        changed_files: list[str],
        developer_summary: str,
        exec_dir: Path,
        role_file: Path,
        state_dir: Path,
        child_env: dict[str, str],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Run an independent, read-only review loop (up to 3 rounds).

        Review execution and validation are shared with the standalone
        ``review`` operation executor. A round that cannot produce an
        explicit valid verdict raises, so the task fails visibly instead of
        being approved by the absence of findings.

        ``session_id`` is the native Runner session of the original developer
        execution. When it is present, the fix round resumes that exact
        session (same state directory, role/runtime and workspace) instead of
        starting a fresh one. The injected ``runner_argv_prefix`` is used for
        both the review and the resume child process.
        """
        review_dir = exec_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        task_artifacts_dir = self.artifact_root / str(task.id)
        task_artifacts_dir.mkdir(parents=True, exist_ok=True)
        (task_artifacts_dir / "review").mkdir(parents=True, exist_ok=True)

        current_summary = developer_summary
        current_changed = list(changed_files)
        review_summary = ""

        # Hot-reload reviewer profile from file in knowledge base if present
        if self.knowledge_base is not None and hasattr(self.knowledge_base, "sync_agent_from_file"):
            try:
                self.knowledge_base.sync_agent_from_file("system-code-reviewer", self.database)
            except Exception as e:
                logger.debug("Could not sync reviewer profile from file: %s", e)

        rev_profile = self._agent_repository.get_current("system-code-reviewer")
        reviewer_system_prompt = rev_profile.system_prompt if rev_profile else ""
        reviewer_runner_config = (
            dict(rev_profile.runner_config)
            if rev_profile and isinstance(rev_profile.runner_config, dict)
            else {}
        )

        executor = ReviewExecutor(
            runner_argv_prefix=self.runner_argv_prefix,
            runtime_defaults=self.runtime_defaults,
            knowledge_base=self.knowledge_base,
            env=child_env,
        )

        for round_num in range(1, 4):
            logger.info("Starting code review round %d for task %s", round_num, task.id)
            request = ReviewRequest(
                task_id=str(task.id),
                task_title=task.title,
                task_description=task.description,
                project_dir=Path(project_dir),
                changed_files=tuple(current_changed),
                developer_summary=current_summary,
                reviewer_system_prompt=reviewer_system_prompt,
                reviewer_runner_config=reviewer_runner_config,
                round_number=round_num,
                state_dir=review_dir / f"state_r{round_num}",
                artifact_dir=review_dir,
                task_artifact_dir=task_artifacts_dir,
            )
            try:
                result = executor.execute(request)
            except ReviewExecutionError:
                logger.error(
                    "Code review round %d for task %s produced no valid verdict; failing task",
                    round_num,
                    task.id,
                    exc_info=True,
                )
                raise

            review_summary = result.report_text
            logger.info("Saved review report for task %s to %s", task.id, result.report_path)

            if result.verdict == VERDICT_APPROVED:
                logger.info("Task %s code review round %d PASSED", task.id, round_num)
                full_summary = (
                    current_summary
                    + f"\n\n### 🔍 Code Review (Раунд {round_num}): ОДОБРЕНО ✅\n"
                    + review_summary[:1500]
                )
                return {"summary": full_summary, "changed_files": current_changed, "needs_user_input": False}

            # Not approved
            logger.warning("Task %s code review round %d requested changes", task.id, round_num)
            if round_num >= 3:
                # Exceeded round limit -> escalate to user
                return {
                    "needs_user_input": True,
                    "summary": current_summary,
                    "clarification": {
                        "title": f"Code Review не согласован после {round_num} раундов",
                        "description": (
                            f"Ревьювер вернул замечания на раунде {round_num}:\n\n"
                            f"{review_summary[:2000]}"
                        ),
                        "question": "Как поступить: принять замечания, завершить задачу как есть или дать указания разработчику?",
                    },
                }

            # Resume developer to address the reviewer's findings
            logger.info("Resuming developer for task %s with reviewer feedback (round %d)", task.id, round_num)
            dev_fix_task = exec_dir / f"dev_fix_task_r{round_num}.md"
            dev_fix_task.write_text(
                f"# Доработка по замечаниям Code Review (Раунд {round_num})\n\n"
                f"Ревьювер проверил твои изменения и вернул следующие замечания:\n\n"
                f"{review_summary}\n\n"
                "Пожалуйста, исправь указанные критические замечания, проверь код тестами и предоставь обновленный отчёт.",
                encoding="utf-8",
            )
            if session_id:
                # Resume the original developer session: the CLI requires the
                # session id and rejects a resume without it. The same state
                # directory, role/runtime and workspace are reused.
                dev_cmd = [
                    *self.runner_argv_prefix,
                    "resume",
                    "--state-dir",
                    str(state_dir),
                    "--session",
                    str(session_id),
                    "--agent",
                    str(role_file),
                    "--task",
                    str(dev_fix_task),
                ]
            else:
                # No resumable native session was reported, so the fix round
                # runs as a fresh execution rather than invoking an invalid
                # ``resume`` command.
                dev_cmd = [
                    *self.runner_argv_prefix,
                    "run",
                    "--agent",
                    str(role_file),
                    "--task",
                    str(dev_fix_task),
                ]
            dev_proc = subprocess.run(
                dev_cmd,
                capture_output=True,
                text=True,
                cwd=str(project_dir),
                env=child_env,
                check=False,
            )
            if dev_proc.returncode != 0:
                raise RuntimeError(
                    "developer fix run failed with exit code "
                    f"{dev_proc.returncode}; review round {round_num} cannot be re-verified"
                )
            try:
                dev_res = extract_runner_result(dev_proc.stdout)
                if dev_res is not None:
                    s_path = dev_res.get("summary_path")
                    if s_path and Path(s_path).is_file():
                        current_summary = Path(s_path).read_text(encoding="utf-8")
                    if dev_res.get("changed_files"):
                        current_changed = list(dev_res["changed_files"])
            except Exception:
                pass

        return {
            "summary": current_summary + "\n\n### 🔍 Code Review: Завершено с замечаниями\n" + review_summary[:1000],
            "changed_files": current_changed,
            "needs_user_input": False,
        }

    def start(self) -> None:
        """Start the background execution loop."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self.run_loop,
                args=(self._stop_event,),
                name=f"task-execution-worker-{self.worker_id}",
                daemon=True,
            )
            logger.info("Starting background TaskExecutionWorker thread: %s", self._thread.name)
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Gracefully stop the worker after current task completes."""
        event = self._stop_event
        if event is not None:
            event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            logger.info("Stopping background TaskExecutionWorker thread: %s", thread.name)
            thread.join(timeout)

    def run_loop(self, stop_event: threading.Event | None = None) -> None:
        """Poll and execute in_progress tasks until stopped."""
        event = stop_event or self._stop_event or threading.Event()
        logger.info("TaskExecutionWorker run_loop started: worker_id=%s", self.worker_id)
        while not event.is_set():
            try:
                outcome = self.step()
            except Exception as loop_error:
                logger.exception(
                    "Unexpected task worker loop error (caught and continuing): %s",
                    loop_error,
                )
                outcome = None

            if outcome is None:
                event.wait(self.poll_interval_seconds)
        logger.info("TaskExecutionWorker run_loop finished: worker_id=%s", self.worker_id)
