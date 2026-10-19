"""Independent, read-only code-review execution.

This module is the single implementation behind both the automatic review
cycle in :mod:`agent_manager.task_worker` and the standalone ``review``
operation executor in :mod:`agent_manager.operation_handlers`. It launches the
``system-code-reviewer`` profile through the same child Runner protocol as the
developer, but with a read-only role configuration and an explicit, validated
verdict.

Truthfulness contract
---------------------

A review round is only *valid* when every one of these holds:

* the child process exited ``0``;
* its last JSON object reports ``status == "completed"``;
* the referenced summary file exists and is non-empty;
* the report carries exactly one explicit verdict
  (``approved`` or ``changes_requested``), either in the canonical text marker
  or in a fenced ``review``/``json`` block.

A nonzero exit, a failed/absent Runner status, empty or malformed output and an
absent or ambiguous verdict are all *failures* and raise
:class:`ReviewExecutionError`. The executor never derives approval from the
absence of P1 findings.

Evidence and artifacts
----------------------

A valid round yields a :class:`ReviewResult` with a domain verdict and a tuple
of evidence-linked :class:`ReviewFinding` objects. The raw report, structured
round metrics and any failure diagnostics are written below the task artifact
directory so the operation result and later analysis (T04) can read them.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .profile_runtime import _build_role_config
from .runtime_defaults import RuntimeDefaultsResolver

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_REVIEWER_PROMPT",
    "READ_ONLY_TOOLS",
    "REVIEWER_AGENT_ID",
    "REVIEW_VERDICTS",
    "VERDICT_APPROVED",
    "VERDICT_CHANGES_REQUESTED",
    "ReviewExecutionError",
    "ReviewExecutor",
    "ReviewFinding",
    "ReviewFormatError",
    "ReviewRequest",
    "ReviewResult",
    "parse_review_report",
]

#: Registry identity of the independent reviewer.
REVIEWER_AGENT_ID = "system-code-reviewer"

#: Domain verdicts stored in the operation ``result`` (never in ``status``).
VERDICT_APPROVED = "approved"
VERDICT_CHANGES_REQUESTED = "changes_requested"
REVIEW_VERDICTS = frozenset({VERDICT_APPROVED, VERDICT_CHANGES_REQUESTED})

#: Reviewer tools. The role is read-only regardless of the stored profile.
READ_ONLY_TOOLS = ("read", "bash")

#: Controls that would grant the reviewer write/administrative power.
_ADMIN_KEYS = ("allow_git_commit", "allow_remote_write", "allow_admin")

#: Severities that make an ``approved`` verdict self-contradictory.
_CRITICAL_SEVERITIES = frozenset({"P1"})

DEFAULT_REVIEWER_PROMPT = (
    "You are an independent, strictly read-only code reviewer. Inspect the "
    "uncommitted changes with `git diff` and `git status`, verify the task "
    "requirements, look for bugs, edge cases, regressions and missing tests. "
    "You must not modify the repository and must not perform any "
    "administrative action.\n\n"
    "Report each problem as an evidence-linked finding on its own line:\n"
    "`P1: <file>:<line> -- what is wrong and how to fix it` (P1 = critical, "
    "P2/P3 = non-blocking).\n\n"
    "End the report with exactly one explicit verdict line:\n"
    "- `VERDICT: APPROVED` / `ВЕРДИКТ: ОДОБРЕНО` when there are no critical "
    "problems;\n"
    "- `VERDICT: CHANGES_REQUESTED` / `ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ` when at "
    "least one P1 finding exists.\n"
)

_APPROVED_MARKERS = ("ВЕРДИКТ: ОДОБРЕНО", "VERDICT: APPROVED")
_CHANGES_MARKERS = (
    "ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ",
    "VERDICT: CHANGES_REQUESTED",
    "VERDICT: CHANGES REQUESTED",
)

_FENCED_JSON_RE = re.compile(
    r"```[ \t]*(?:review|json)[ \t]*\r?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_FINDING_RE = re.compile(
    r"^\s*(P[1-3]|BLOCKER|CRITICAL|MAJOR|MINOR|INFO)\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_FILE_LINE_RE = re.compile(
    r"([A-Za-z0-9_][A-Za-z0-9_./\-]*\.[A-Za-z0-9_]+)[:\s]+(\d+)"
)


class ReviewExecutionError(Exception):
    """A review round could not produce a valid, validated result.

    ``diagnostics`` is a JSON-serializable mapping with the child exit status,
    bounded stdout/stderr excerpts and the failure reason. When the executor
    managed to write it, ``diagnostics_path`` points at the persisted JSON
    artifact on disk.
    """

    def __init__(
        self,
        message: str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
        diagnostics_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics: dict[str, Any] = dict(diagnostics or {})
        self.diagnostics_path = diagnostics_path


class ReviewFormatError(ReviewExecutionError):
    """The reviewer report is present but cannot be validated."""


@dataclasses.dataclass(frozen=True)
class ReviewFinding:
    """One evidence-linked reviewer finding."""

    severity: str
    message: str
    file: str | None = None
    line: int | None = None
    evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "evidence": self.evidence,
        }


@dataclasses.dataclass(frozen=True)
class ReviewResult:
    """A validated domain review result for one round."""

    task_id: str
    reviewer_agent_id: str
    verdict: str
    findings: tuple[ReviewFinding, ...]
    report_text: str
    report_path: str
    metrics_path: str
    round_number: int
    duration_ms: float
    returncode: int
    runtime: Mapping[str, Any]

    @property
    def approved(self) -> bool:
        """True only for the explicit ``approved`` domain verdict."""
        return self.verdict == VERDICT_APPROVED

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready result stored in the operation record and task meta."""
        return {
            "task_id": self.task_id,
            "reviewer_agent_id": self.reviewer_agent_id,
            "verdict": self.verdict,
            "approved": self.approved,
            "findings": [finding.to_dict() for finding in self.findings],
            "report": self.report_text,
            "report_path": self.report_path,
            "metrics_path": self.metrics_path,
            "round": self.round_number,
            "duration_ms": self.duration_ms,
            "returncode": self.returncode,
            "runtime": dict(self.runtime),
        }


@dataclasses.dataclass(frozen=True)
class ReviewRequest:
    """Everything one review round needs, assembled by the caller."""

    task_id: str
    task_title: str
    task_description: str | None
    project_dir: Path
    changed_files: Sequence[str] = ()
    developer_summary: str = ""
    reviewer_system_prompt: str = ""
    reviewer_runner_config: Mapping[str, Any] = dataclasses.field(
        default_factory=dict
    )
    focus: str = ""
    round_number: int = 1
    state_dir: Path | None = None
    artifact_dir: Path | None = None
    task_artifact_dir: Path | None = None
    timeout_seconds: float | None = None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_last_json_object(stdout: str) -> dict[str, Any] | None:
    """Return the last complete JSON object line, or ``None``."""
    for line in reversed(stdout.strip().splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return None


def _normalize_verdict(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"approved", "approve", "одобрено"}:
        return VERDICT_APPROVED
    if text in {
        "changes_requested",
        "changes requested",
        "changes-requested",
        "требуются исправления",
    }:
        return VERDICT_CHANGES_REQUESTED
    raise ReviewFormatError(f"unknown review verdict: {value!r}")


def _normalize_severity(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"P1", "BLOCKER", "CRITICAL"}:
        return "P1"
    if text in {"P2", "MAJOR"}:
        return "P2"
    if text in {"P3", "MINOR", "INFO"}:
        return "P3"
    return text or "P1"


def _file_line_from_text(text: str) -> tuple[str | None, int | None]:
    match = _FILE_LINE_RE.search(text)
    if match is None:
        return None, None
    try:
        return match.group(1), int(match.group(2))
    except (TypeError, ValueError):
        return match.group(1), None


def _findings_from_json(payload: Mapping[str, Any]) -> list[ReviewFinding]:
    raw_findings = payload.get("findings") or []
    if not isinstance(raw_findings, Sequence) or isinstance(
        raw_findings, (str, bytes)
    ):
        raise ReviewFormatError("review report findings must be a list")
    findings: list[ReviewFinding] = []
    for item in raw_findings:
        if not isinstance(item, Mapping):
            raise ReviewFormatError(
                "each review finding must be a JSON object"
            )
        message = str(
            item.get("message") or item.get("description") or ""
        ).strip()
        if not message:
            raise ReviewFormatError("review finding is missing a message")
        severity_raw = item.get("severity")
        if severity_raw is None or str(severity_raw).strip() == "":
            raise ReviewFormatError("review finding is missing a severity")
        line_value = item.get("line")
        line: int | None = (
            int(line_value) if isinstance(line_value, (int, float)) else None
        )
        evidence = item.get("evidence")
        findings.append(
            ReviewFinding(
                severity=_normalize_severity(severity_raw),
                message=message,
                file=(str(item["file"]) if item.get("file") else None),
                line=line,
                evidence=(str(evidence) if evidence else None),
            )
        )
    return findings


def _findings_from_text(report_text: str) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for line in report_text.splitlines():
        match = _FINDING_RE.match(line)
        if match is None:
            continue
        body = match.group(2).strip()
        file_name, line_no = _file_line_from_text(body)
        findings.append(
            ReviewFinding(
                severity=_normalize_severity(match.group(1)),
                message=body,
                file=file_name,
                line=line_no,
                evidence=line.strip(),
            )
        )
    return findings


def parse_review_report(report_text: str) -> tuple[str, tuple[ReviewFinding, ...]]:
    """Validate one reviewer report and return ``(verdict, findings)``.

    A fenced ``review``/``json`` block is authoritative when present; a
    malformed block fails instead of silently falling back. Otherwise the
    canonical explicit verdict markers are required. An absent or ambiguous
    verdict, an ``approved`` verdict with critical findings, and a
    ``changes_requested`` verdict without any evidence-linked finding are all
    typed :class:`ReviewFormatError` failures.
    """
    text = (report_text or "").strip()
    if not text:
        raise ReviewFormatError("review report is empty")

    blocks = _FENCED_JSON_RE.findall(text)
    if blocks:
        raw_block = blocks[-1].strip()
        try:
            payload = json.loads(raw_block)
        except json.JSONDecodeError as error:
            raise ReviewFormatError(
                f"review report JSON block is malformed: {error}"
            ) from error
        if not isinstance(payload, Mapping):
            raise ReviewFormatError("review report JSON block must be an object")
        if "verdict" not in payload:
            raise ReviewFormatError(
                "review report JSON block has no verdict"
            )
        verdict = _normalize_verdict(payload.get("verdict"))
        findings = _findings_from_json(payload)
    else:
        upper = text.upper()
        approved = any(marker in upper for marker in _APPROVED_MARKERS)
        changes = any(marker in upper for marker in _CHANGES_MARKERS)
        if approved and changes:
            raise ReviewFormatError(
                "review report carries both approved and changes_requested "
                "verdict markers"
            )
        if not approved and not changes:
            raise ReviewFormatError(
                "review report has no explicit verdict marker"
            )
        verdict = VERDICT_APPROVED if approved else VERDICT_CHANGES_REQUESTED
        findings = _findings_from_text(text)

    critical = [f for f in findings if f.severity in _CRITICAL_SEVERITIES]
    if verdict == VERDICT_APPROVED and critical:
        raise ReviewFormatError(
            "review verdict is approved but the report contains critical "
            "findings"
        )
    if verdict == VERDICT_CHANGES_REQUESTED and not findings:
        raise ReviewFormatError(
            "review verdict changes_requested has no evidence-linked findings"
        )
    return verdict, tuple(findings)


class ReviewExecutor:
    """Run one validated review round with a read-only reviewer role.

    The executor owns the trusted Runner argv prefix, the optional runtime
    defaults resolver and the optional knowledge base. A request supplies the
    task, workspace and artifact paths. The child process is launched with
    :func:`subprocess.run` unless a test injects ``subprocess_run``.
    """

    def __init__(
        self,
        *,
        runner_argv_prefix: Sequence[str],
        runtime_defaults: RuntimeDefaultsResolver | None = None,
        knowledge_base: Any | None = None,
        env: Mapping[str, str] | None = None,
        subprocess_run: Any | None = None,
    ) -> None:
        if not runner_argv_prefix:
            raise ValueError("runner_argv_prefix must not be empty")
        self._runner_argv_prefix = tuple(runner_argv_prefix)
        self._runtime_defaults = runtime_defaults or RuntimeDefaultsResolver()
        self._knowledge_base = knowledge_base
        self._env = dict(env) if env is not None else None
        self._subprocess_run = subprocess_run

    @property
    def runtime_defaults(self) -> RuntimeDefaultsResolver:
        """The resolver used to select the reviewer provider/model."""
        return self._runtime_defaults

    # -- role construction ----------------------------------------------

    def _build_review_role_config(
        self,
        request: ReviewRequest,
        *,
        state_dir: Path,
    ) -> dict[str, Any]:
        """Build a strictly read-only reviewer role from the stored profile."""
        role_config = dict(request.reviewer_runner_config or {})
        for key in _ADMIN_KEYS:
            role_config.pop(key, None)
        role_config["agent_id"] = REVIEWER_AGENT_ID
        role_config["project"] = str(request.project_dir)
        role_config["state_dir"] = str(state_dir)
        role_config["tools"] = list(READ_ONLY_TOOLS)
        role_config["project_read_only"] = True
        role_config["read_only"] = True

        readonly_paths = [str(path) for path in role_config.get("readonly_paths") or []]
        knowledge_base = self._knowledge_base
        if knowledge_base is not None:
            root_dir = getattr(knowledge_base, "root_dir", None)
            if root_dir is not None and Path(root_dir).is_dir():
                kb_root = str(root_dir)
                if kb_root not in readonly_paths:
                    readonly_paths.append(kb_root)
        if readonly_paths:
            role_config["readonly_paths"] = readonly_paths
        return role_config

    # -- prompt ----------------------------------------------------------

    def _build_review_prompt(self, request: ReviewRequest) -> str:
        changed = "\n".join(f"- {path}" for path in request.changed_files) or "- (none reported)"
        focus = request.focus.strip()
        focus_block = (
            f"\n## Дополнительный фокус ревью\n{focus}\n" if focus else ""
        )
        return (
            f"# Code Review: Task #{request.task_id} — {request.task_title} "
            f"(Раунд {request.round_number})\n\n"
            "Роль: независимый Code Reviewer. Изучи незакоммиченные изменения "
            "`git diff` строго в режиме read-only.\n\n"
            f"## Исходная задача:\n{request.task_description or request.task_title}\n\n"
            f"## Отчёт разработчика:\n{request.developer_summary}\n\n"
            f"## Изменённые файлы:\n{changed}\n"
            f"{focus_block}\n"
            "## Инструкция по проверке:\n"
            "1. Изучи `git diff` / `git status` и исходные файлы.\n"
            "2. Проверь наличие багов, краевых случаев, соответствие ТЗ и качество тестов.\n"
            "3. Ты работаешь строго read-only и не выполняешь административных действий.\n"
            "4. Сформулируй вердикт. Критические замечания опиши как "
            "`P1: файл:строка — суть проблемы и как исправить`.\n"
            "   В конце обязательно укажи ровно одну строку вердикта:\n"
            "   `ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ` при наличии P1;\n"
            "   `ВЕРДИКТ: ОДОБРЕНО`, если критических замечаний нет.\n"
        )

    # -- execution -------------------------------------------------------

    def execute(self, request: ReviewRequest) -> ReviewResult:
        """Run and validate one review round, persisting its artifacts.

        Raises:
            ReviewExecutionError: for a nonzero exit, failed Runner status,
                empty/malformed output, an absent/invalid verdict or any
                missing report. The diagnostics are persisted to disk first.
        """
        started = time.perf_counter()
        if request.artifact_dir is None:
            raise ValueError("ReviewRequest.artifact_dir is required")
        artifact_dir = Path(request.artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        task_artifact_dir = (
            Path(request.task_artifact_dir)
            if request.task_artifact_dir
            else artifact_dir
        )
        review_artifact_dir = task_artifact_dir / "review"
        review_artifact_dir.mkdir(parents=True, exist_ok=True)
        round_number = int(request.round_number)
        state_dir = (
            Path(request.state_dir)
            if request.state_dir
            else artifact_dir / f"state_r{round_number}"
        )
        state_dir.mkdir(parents=True, exist_ok=True)

        system_prompt_path = artifact_dir / f"reviewer_system_prompt_r{round_number}.md"
        system_prompt_path.write_text(
            request.reviewer_system_prompt or DEFAULT_REVIEWER_PROMPT,
            encoding="utf-8",
        )
        task_prompt_path = artifact_dir / f"review_prompt_r{round_number}.md"
        task_prompt_path.write_text(
            self._build_review_prompt(request), encoding="utf-8"
        )

        role_config = self._build_review_role_config(request, state_dir=state_dir)
        runtime_selection = self._runtime_defaults.resolve(role_config)
        role_config = runtime_selection.role_config
        role_config = _build_role_config(
            role_config,
            agent_id=REVIEWER_AGENT_ID,
            prompt_path=system_prompt_path,
        )
        role_path = artifact_dir / f"role_r{round_number}.json"
        role_path.write_text(
            json.dumps(role_config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        runtime_payload: dict[str, Any] = {
            "provider": runtime_selection.provider,
            "model": runtime_selection.model,
            "pi_auth_provider": runtime_selection.pi_auth_provider,
            "models_file": runtime_selection.models_file,
            "selection": runtime_selection.selection_dict(),
        }

        cmd = [
            *self._runner_argv_prefix,
            "run",
            "--agent",
            str(role_path),
            "--task",
            str(task_prompt_path),
        ]
        try:
            proc = self._invoke(cmd, cwd=request.project_dir, timeout=request.timeout_seconds)
        except Exception as error:  # noqa: BLE001 - recorded as diagnostics
            duration_ms = (time.perf_counter() - started) * 1000.0
            raise self._failure(
                request,
                round_number,
                review_artifact_dir,
                reason="runner_launch_failed",
                message=f"failed to launch reviewer: {type(error).__name__}: {error}",
                duration_ms=duration_ms,
                runtime=runtime_payload,
            ) from error

        duration_ms = (time.perf_counter() - started) * 1000.0

        if proc.returncode != 0:
            raise self._failure(
                request,
                round_number,
                review_artifact_dir,
                reason="runner_nonzero_exit",
                message=(
                    f"reviewer process exited with code {proc.returncode}"
                ),
                duration_ms=duration_ms,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                runtime=runtime_payload,
            )

        payload = _extract_last_json_object(proc.stdout or "")
        if payload is None:
            raise self._failure(
                request,
                round_number,
                review_artifact_dir,
                reason="malformed_runner_output",
                message="reviewer produced no parseable JSON result object",
                duration_ms=duration_ms,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                runtime=runtime_payload,
            )

        status = payload.get("status")
        if status != "completed":
            raise self._failure(
                request,
                round_number,
                review_artifact_dir,
                reason="runner_status_not_completed",
                message=f"reviewer reported status {status!r}",
                duration_ms=duration_ms,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                runtime=runtime_payload,
            )

        summary_path_raw = payload.get("summary_path")
        summary_path = Path(summary_path_raw) if summary_path_raw else None
        if summary_path is None or not summary_path.is_file():
            raise self._failure(
                request,
                round_number,
                review_artifact_dir,
                reason="missing_report",
                message=(
                    "reviewer completed without a readable report file: "
                    f"{summary_path_raw!r}"
                ),
                duration_ms=duration_ms,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                runtime=runtime_payload,
            )

        try:
            report_text = summary_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise self._failure(
                request,
                round_number,
                review_artifact_dir,
                reason="unreadable_report",
                message=f"cannot read reviewer report: {type(error).__name__}: {error}",
                duration_ms=duration_ms,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                runtime=runtime_payload,
            ) from error

        if not report_text:
            raise self._failure(
                request,
                round_number,
                review_artifact_dir,
                reason="empty_report",
                message="reviewer report is empty",
                duration_ms=duration_ms,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                runtime=runtime_payload,
            )

        try:
            verdict, findings = parse_review_report(report_text)
        except ReviewFormatError as error:
            raise self._failure(
                request,
                round_number,
                review_artifact_dir,
                reason="invalid_report",
                message=str(error),
                duration_ms=duration_ms,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                runtime=runtime_payload,
            ) from error

        report_path = review_artifact_dir / f"round_{round_number}.md"
        report_path.write_text(report_text, encoding="utf-8")
        # Keep the historical flat report name as well for existing readers.
        (task_artifact_dir / f"review_round_{round_number}.md").write_text(
            report_text, encoding="utf-8"
        )

        metrics = {
            "task_id": request.task_id,
            "round": round_number,
            "verdict": verdict.upper(),
            "domain_verdict": verdict,
            "approved": verdict == VERDICT_APPROVED,
            "findings": [finding.to_dict() for finding in findings],
            "returncode": proc.returncode,
            "report_file": str(report_path),
            "duration_ms": duration_ms,
            "timestamp": _utcnow_iso(),
            "reviewer_agent_id": REVIEWER_AGENT_ID,
            "runtime": runtime_payload,
        }
        metrics_path = review_artifact_dir / f"round_{round_number}.json"
        metrics_path.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info(
            "review round completed task=%s round=%d verdict=%s findings=%d",
            request.task_id,
            round_number,
            verdict,
            len(findings),
            extra={
                "event": "review_round_completed",
                "task_id": request.task_id,
                "round": round_number,
                "verdict": verdict,
                "findings": len(findings),
            },
        )
        return ReviewResult(
            task_id=request.task_id,
            reviewer_agent_id=REVIEWER_AGENT_ID,
            verdict=verdict,
            findings=findings,
            report_text=report_text,
            report_path=str(report_path),
            metrics_path=str(metrics_path),
            round_number=round_number,
            duration_ms=duration_ms,
            returncode=proc.returncode,
            runtime=runtime_payload,
        )

    # -- internals -------------------------------------------------------

    def _invoke(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        timeout: float | None,
    ) -> Any:
        run = self._subprocess_run or subprocess.run
        env = dict(self._env) if self._env is not None else None
        return run(
            list(cmd),
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
            check=False,
            timeout=timeout,
        )

    def _failure(
        self,
        request: ReviewRequest,
        round_number: int,
        review_artifact_dir: Path,
        *,
        reason: str,
        message: str,
        duration_ms: float,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        runtime: Mapping[str, Any] | None = None,
    ) -> ReviewExecutionError:
        """Persist one failure diagnostic and return the typed error."""
        diagnostics: dict[str, Any] = {
            "task_id": request.task_id,
            "round": round_number,
            "reason": reason,
            "message": message,
            "returncode": returncode,
            "stdout": (stdout or "")[:4000],
            "stderr": (stderr or "")[:4000],
            "duration_ms": duration_ms,
            "timestamp": _utcnow_iso(),
            "reviewer_agent_id": REVIEWER_AGENT_ID,
            "runtime": dict(runtime or {}),
        }
        diagnostics_path = (
            review_artifact_dir / f"round_{round_number}_failure.json"
        )
        try:
            diagnostics_path.write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:  # pragma: no cover - artifact write is best effort
            logger.warning(
                "could not persist review failure diagnostics for task=%s",
                request.task_id,
                exc_info=True,
            )
        logger.warning(
            "review round failed task=%s round=%d reason=%s: %s",
            request.task_id,
            round_number,
            reason,
            message,
            extra={
                "event": "review_round_failed",
                "task_id": request.task_id,
                "round": round_number,
                "reason": reason,
            },
        )
        return ReviewExecutionError(
            message,
            diagnostics=diagnostics,
            diagnostics_path=str(diagnostics_path),
        )
