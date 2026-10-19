"""Git Engine for safe, atomic commits, rollbacks, and CI monitoring.

Enforces system security rules:
- NEVER uses catch-all 'git add .' or staging of untracked sensitive files.
- Always stages changed files explicitly by relative path.
- Handles rollbacks cleanly via git restore / file unlink.
- Monitors GitHub Actions workflows using the gh CLI.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
import subprocess
from typing import Sequence

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class GitEngineResult:
    """Outcome of a git or CI operation."""

    success: bool
    action: str
    message: str
    commit_hash: str | None = None
    branch: str | None = None
    ci_status: str | None = None  # e.g. "success", "failure", "skipped", "timeout"
    ci_logs: str | None = None


class GitEngine:
    """Executes safe git operations within a workspace directory."""

    def __init__(self, default_workspace: Path | str | None = None) -> None:
        self.default_workspace = Path(default_workspace) if default_workspace else None

    def _resolve_workspace(self, workspace: Path | str | None) -> Path:
        ws = Path(workspace) if workspace else self.default_workspace
        if not ws:
            raise ValueError("No workspace specified and no default workspace configured.")
        ws = ws.resolve()
        if not ws.is_dir():
            raise FileNotFoundError(f"Workspace directory does not exist: {ws}")
        return ws

    def _run_cmd(
        self,
        args: list[str],
        cwd: Path,
        check: bool = False,
        capture_output: bool = True,
        timeout: float | None = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
        )

    def get_current_branch(self, workspace: Path | str | None = None) -> str:
        """Return the active git branch name in the workspace."""
        ws = self._resolve_workspace(workspace)
        proc = self._run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ws, check=True)
        return proc.stdout.strip()

    def commit_local(
        self,
        workspace: Path | str | None,
        task_id: str,
        title: str,
        summary: str | None = None,
        changed_files: Sequence[str] | None = None,
        custom_message: str | None = None,
    ) -> GitEngineResult:
        """Safely stage and commit only explicitly changed files."""
        ws = self._resolve_workspace(workspace)
        files_to_stage = [f for f in (changed_files or []) if f.strip()]

        if not files_to_stage:
            # Fallback: check git status -s for modified/untracked files
            status_proc = self._run_cmd(["git", "status", "--porcelain"], cwd=ws)
            if status_proc.returncode == 0:
                for line in status_proc.stdout.splitlines():
                    if len(line) > 3:
                        file_path = line[3:].strip()
                        if not file_path.startswith("localdoc/") and not file_path.startswith(".gemini/"):
                            files_to_stage.append(file_path)

        if not files_to_stage:
            return GitEngineResult(
                success=False,
                action="commit_local",
                message="Нет файлов для коммита в рабочей директории.",
            )

        # 1. Explicitly stage files (NEVER git add .)
        stage_args = ["git", "add", "--"] + files_to_stage
        stage_proc = self._run_cmd(stage_args, cwd=ws)
        if stage_proc.returncode != 0:
            logger.error("git add failed: %s", stage_proc.stderr)
            return GitEngineResult(
                success=False,
                action="commit_local",
                message=f"Ошибка добавления файлов в индекс: {stage_proc.stderr.strip()}",
            )

        # 2. Format commit message
        if custom_message:
            commit_msg = custom_message.strip()
        else:
            summary_part = f"\n\n{summary.strip()}" if summary and summary.strip() else ""
            commit_msg = f"feat(#{task_id}): {title.strip()}{summary_part}"

        # 3. Commit
        commit_proc = self._run_cmd(["git", "commit", "-m", commit_msg], cwd=ws)
        if commit_proc.returncode != 0:
            if "nothing to commit" in commit_proc.stdout.lower() or "nothing to commit" in commit_proc.stderr.lower():
                return GitEngineResult(
                    success=True,
                    action="commit_local",
                    message="Нет новых изменений для фиксации (рабочая директория чиста).",
                )
            logger.error("git commit failed: %s", commit_proc.stderr)
            return GitEngineResult(
                success=False,
                action="commit_local",
                message=f"Ошибка создания коммита: {commit_proc.stderr.strip()}",
            )

        # 4. Get commit hash
        hash_proc = self._run_cmd(["git", "rev-parse", "HEAD"], cwd=ws)
        commit_hash = (hash_proc.stdout or "").strip() if (hash_proc.returncode == 0 and hash_proc.stdout) else None
        branch = self.get_current_branch(ws)

        return GitEngineResult(
            success=True,
            action="commit_local",
            message=f"Коммит {commit_hash[:8] if commit_hash else ''} успешно создан в ветке {branch}.",
            commit_hash=commit_hash,
            branch=branch,
        )

    def rollback(
        self,
        workspace: Path | str | None,
        changed_files: Sequence[str] | None = None,
    ) -> GitEngineResult:
        """Safely discard staged and working tree modifications."""
        ws = self._resolve_workspace(workspace)
        files_to_restore = [f for f in (changed_files or []) if f.strip()]

        if not files_to_restore:
            # Fallback: check porcelain status
            status_proc = self._run_cmd(["git", "status", "--porcelain"], cwd=ws)
            if status_proc.returncode == 0:
                for line in status_proc.stdout.splitlines():
                    if len(line) > 3:
                        files_to_restore.append(line[3:].strip())

        restored_files = []
        for file_rel in files_to_restore:
            abs_path = (ws / file_rel).resolve()
            # If file is tracked or staged, restore it
            self._run_cmd(["git", "restore", "--staged", "--worktree", "--", file_rel], cwd=ws)
            # If file still exists and was untracked, remove it safely
            if abs_path.is_file():
                # Check if still untracked
                check_tracked = self._run_cmd(["git", "ls-files", "--error-unmatch", file_rel], cwd=ws)
                if check_tracked.returncode != 0:
                    try:
                        abs_path.unlink()
                        restored_files.append(file_rel)
                    except OSError as exc:
                        logger.warning("Could not remove untracked file %s: %s", abs_path, exc)
            else:
                restored_files.append(file_rel)

        return GitEngineResult(
            success=True,
            action="rollback",
            message="Изменения успешно откатаны. Рабочая директория возвращена к исходному состоянию.",
        )

    def commit_and_push(
        self,
        workspace: Path | str | None,
        task_id: str,
        title: str,
        summary: str | None = None,
        changed_files: Sequence[str] | None = None,
        custom_message: str | None = None,
        remote: str = "origin",
        watch_ci: bool = True,
        ci_timeout_sec: int = 600,
    ) -> GitEngineResult:
        """Commit changes locally, push to remote, and optionally monitor GitHub Actions."""
        ws = self._resolve_workspace(workspace)

        # 1. Local commit
        commit_res = self.commit_local(
            workspace=ws,
            task_id=task_id,
            title=title,
            summary=summary,
            changed_files=changed_files,
            custom_message=custom_message,
        )
        if not commit_res.success:
            return commit_res

        branch = commit_res.branch or self.get_current_branch(ws)

        # 2. Git push
        push_proc = self._run_cmd(["git", "push", remote, branch], cwd=ws)
        if push_proc.returncode != 0:
            logger.error("git push failed: %s", push_proc.stderr)
            return GitEngineResult(
                success=False,
                action="commit_push",
                message=f"Коммит создан, но ошибка при отправке в репозиторий: {push_proc.stderr.strip()}",
                commit_hash=commit_res.commit_hash,
                branch=branch,
            )

        if not watch_ci:
            return GitEngineResult(
                success=True,
                action="commit_push",
                message=f"Изменения успешно закоммичены и отправлены в {remote}/{branch}.",
                commit_hash=commit_res.commit_hash,
                branch=branch,
            )

        # 3. GitHub Actions CI Monitoring
        ci_outcome = self._monitor_github_actions(ws, branch=branch, timeout_sec=ci_timeout_sec)
        return GitEngineResult(
            success=(ci_outcome.get("status") == "success"),
            action="commit_push",
            message=ci_outcome.get("message", "CI завершён."),
            commit_hash=commit_res.commit_hash,
            branch=branch,
            ci_status=ci_outcome.get("status"),
            ci_logs=ci_outcome.get("logs"),
        )

    def _monitor_github_actions(
        self,
        workspace: Path,
        branch: str,
        timeout_sec: int = 600,
    ) -> dict[str, str | None]:
        """Query and monitor GitHub Actions workflow runs via gh CLI."""
        # Find run ID for current branch
        list_proc = self._run_cmd(
            ["gh", "run", "list", "--branch", branch, "--limit", "1", "--json", "databaseId,status,conclusion"],
            cwd=workspace,
        )
        if list_proc.returncode != 0:
            logger.warning("gh run list failed: %s", list_proc.stderr)
            return {
                "status": "unknown",
                "message": "Изменения отправлены в GitHub, но не удалось получить статус CI через gh CLI.",
                "logs": None,
            }

        try:
            runs = json.loads(list_proc.stdout)
            if not runs:
                return {
                    "status": "not_found",
                    "message": "Изменения отправлены в GitHub (активных Actions workflows для ветки не обнаружено).",
                    "logs": None,
                }
            run_id = str(runs[0].get("databaseId"))
        except Exception as exc:
            logger.warning("Could not parse gh run list: %s", exc)
            return {
                "status": "unknown",
                "message": "Изменения отправлены, статус CI неизвестен.",
                "logs": None,
            }

        # Watch run to completion
        logger.info("Watching GitHub Actions run %s on branch %s...", run_id, branch)
        watch_proc = self._run_cmd(
            ["gh", "run", "watch", run_id, "--exit-status"],
            cwd=workspace,
            timeout=float(timeout_sec),
        )

        if watch_proc.returncode == 0:
            return {
                "status": "success",
                "message": f"✅ GitHub Actions успешно пройдены! (Run #{run_id})",
                "logs": None,
            }

        # Failure: capture failed logs
        view_proc = self._run_cmd(
            ["gh", "run", "view", run_id, "--log-failed"],
            cwd=workspace,
            timeout=30.0,
        )
        failed_logs = view_proc.stdout[:2000] if view_proc.returncode == 0 else watch_proc.stderr[:1000]

        return {
            "status": "failure",
            "message": f"❌ Сбой GitHub Actions в запуске #{run_id}.",
            "logs": failed_logs,
        }
