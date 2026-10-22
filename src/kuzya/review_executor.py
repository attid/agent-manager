"""Independent, read-only code-review execution.

This module is the single implementation behind both the automatic review
cycle in :mod:`kuzya.task_worker` and the standalone ``review``
operation executor in :mod:`kuzya.operation_handlers`. It launches the
``system-code-reviewer`` profile through the same child Runner protocol as the
developer, but with a read-only role configuration and an explicit, validated
verdict.

Truthfulness contract
---------------------

A review round requires a successful process and a complete validated JSON
result. Pi additionally requires completed Runner status and a readable summary
file containing that JSON. Native engines return the JSON on stdout. Text
markers and fenced blocks are never consulted by the executor.

Missing, empty or invalid results allow at most three format attempts per
review round. Process, launch and Runner status failures stop immediately.
An exhausted round raises :class:`ReviewExecutionError`, never approval.

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
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capabilities import write_handbook_discovery
from .profile_runtime import _build_role_config
from .runner_output import extract_runner_result
from .runtime_defaults import RuntimeDefaultsResolver

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_REVIEWER_PROMPT",
    "READ_ONLY_TOOLS",
    "REVIEWER_AGENT_ID",
    "REVIEW_VERDICTS",
    "VERDICT_APPROVED",
    "VERDICT_CHANGES_REQUESTED",
    "VERDICT_SOURCE_ANYWHERE",
    "VERDICT_SOURCE_FINAL_LINE",
    "AgyReviewerBackend",
    "CodexReviewerBackend",
    "PiReviewerBackend",
    "ReviewExecutionError",
    "ReviewExecutor",
    "ReviewFinding",
    "ReviewFormatError",
    "ReviewRequest",
    "ReviewResult",
    "find_reviewer_binary",
    "parse_review_report",
    "parse_review_json",
]

#: Default timeout for external reviewer engines that have no Runner contract.
#: A real review round explores the repository and reasons over the diff;
#: three minutes killed legitimate runs, so the floor is ten minutes.
DEFAULT_EXTERNAL_REVIEW_TIMEOUT_SECONDS = 600.0

#: Supported values of ``runner_config.engine``.
REVIEW_ENGINES = frozenset({"pi", "agy", "codex"})

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

MAX_FORMAT_ATTEMPTS = 3
REVIEW_JSON_INSTRUCTIONS = """
## Required JSON result
Return exactly one JSON object without Markdown fences or surrounding text.
For Pi, put this JSON in the summary file; native engines return it on stdout.
Required fields: verdict ("approved" or "changes_requested"), findings (array).
Each finding requires severity (P1/P2/P3), file (non-empty path), line (positive
integer), message (non-empty explanation and correction). Optional evidence is
a string; optional report is a human-readable string.
Use changes_requested when a P1 exists; approved cannot contain P1.
changes_requested requires at least one finding. Do not modify project files.
Example: {"verdict":"approved","findings":[],"report":"No blocking defects found."}
This JSON contract overrides earlier instructions about text verdict markers.
"""

DEFAULT_REVIEWER_PROMPT = (
    "You are an independent, strictly read-only code reviewer. Inspect "
    "uncommitted changes, requirements, bugs, regressions and missing tests. "
    "Do not modify the repository or perform administrative actions. "
    "Return the JSON result defined by the task contract."
) + REVIEW_JSON_INSTRUCTIONS

#: Legacy text-parser options, retained for callers of parse_review_report.
#: ReviewExecutor uses only parse_review_json and never these markers.
VERDICT_SOURCE_FINAL_LINE = "final_line"
VERDICT_SOURCE_ANYWHERE = "anywhere"
_VERDICT_SOURCES = frozenset(
    {VERDICT_SOURCE_FINAL_LINE, VERDICT_SOURCE_ANYWHERE}
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
_LIST_ITEM_RE = re.compile(r"^[-*+]\s+")
_FINDING_RE = re.compile(
    r"^(?P<severity>P[1-3]|BLOCKER|CRITICAL|MAJOR|MINOR|INFO)"
    r"\s*(?:\*\*|__|`)?\s*"
    r"(?P<separator>[:\-\u2013\u2014])\s*"
    r"(?:\*\*|__|`)?\s*"
    r"(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_FINDING_MARKUP = ("**", "__", "`")
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
    result_path: str

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
            "result_path": self.result_path,
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

    @property
    def engine(self) -> str:
        """The configured reviewer engine, defaulting to the Pi runner."""
        config = self.reviewer_runner_config or {}
        if not isinstance(config, Mapping):
            return "pi"
        value = config.get("engine")
        return str(value or "pi").strip().lower() or "pi"


def find_reviewer_binary(engine: str) -> Path | None:
    """Locate an external reviewer binary on ``PATH`` or in ``~/.local/bin``.

    ``shutil.which`` already verifies that a ``PATH`` hit is executable, so it
    is trusted as-is. The conventional user-local location is checked only
    when it is a real executable file.
    """
    discovered = shutil.which(engine)
    if discovered:
        return Path(discovered)
    fallback = Path.home() / ".local" / "bin" / engine
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return fallback
    return None


@dataclasses.dataclass(frozen=True)
class _ReviewerRunContext:
    """Prepared, engine-independent inputs for one review round."""

    request: ReviewRequest
    round_number: int
    review_artifact_dir: Path
    task_prompt_path: Path
    prompt_text: str
    role_path: Path
    runtime_payload: Mapping[str, Any]


@dataclasses.dataclass(frozen=True)
class _ReviewerBackendResult:
    """The output one reviewer backend produced for the executor to validate."""

    report_text: str
    returncode: int
    runtime: Mapping[str, Any]
    stdout: str = ""
    stderr: str = ""


class _ReviewerBackendFailure(Exception):
    """An internal, typed failure that the executor persists as diagnostics."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.cause = cause


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_last_json_object(stdout: str) -> dict[str, Any] | None:
    """Return the last JSON object emitted on ``stdout``, or ``None``.

    The Runner CLI prints a single pretty-printed JSON document, so the whole
    output is parsed before falling back to the last balanced object.
    """
    return extract_runner_result(stdout)


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


def _normalize_finding_line(line: str) -> str:
    """Remove ordinary Markdown decoration around a finding line.

    Reviewers commonly emit findings as list items wrapped in inline code,
    bold or ``__bold__``. The severity itself may be the only decorated part
    (``**P1**: ...``). Stripping the wrappers before matching keeps the
    finding grammar readable; it never relaxes the explicit verdict rules.
    """
    text = _LIST_ITEM_RE.sub("", line.strip(), count=1).strip()
    for marker in _FINDING_MARKUP:
        if text.startswith(marker):
            text = text[len(marker):].strip()
            if text.endswith(marker):
                text = text[: -len(marker)].strip()
            break
    return text


def _findings_from_text(report_text: str) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for line in report_text.splitlines():
        match = _FINDING_RE.match(_normalize_finding_line(line))
        if match is None:
            continue
        body = match.group("body").strip().rstrip("*_`").strip()
        if not body:
            continue
        file_name, line_no = _file_line_from_text(body)
        findings.append(
            ReviewFinding(
                severity=_normalize_severity(match.group("severity")),
                message=body,
                file=file_name,
                line=line_no,
                evidence=line.strip(),
            )
        )
    return findings


def _verdict_from_markers(text: str) -> str:
    """Return the verdict from any explicit marker in the whole report."""
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
    return VERDICT_APPROVED if approved else VERDICT_CHANGES_REQUESTED


def _verdict_from_final_line(text: str) -> str:
    """Return the verdict from the last explicit verdict line in the report.

    External reviewer engines receive a prompt that names both verdicts as
    schema examples, and a model may quote that prompt or a reviewed file
    containing the markers. Scanning backwards for the final verdict line makes
    the report suffix authoritative while still rejecting a genuinely
    ambiguous final line that carries both verdicts.
    """
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        approved = any(marker in upper for marker in _APPROVED_MARKERS)
        changes = any(marker in upper for marker in _CHANGES_MARKERS)
        if not approved and not changes:
            continue
        if approved and changes:
            raise ReviewFormatError(
                "last review verdict line carries both approved and "
                "changes_requested verdict markers"
            )
        return VERDICT_APPROVED if approved else VERDICT_CHANGES_REQUESTED
    raise ReviewFormatError("review report has no explicit verdict marker")


def parse_review_report(
    report_text: str,
    *,
    verdict_source: str = VERDICT_SOURCE_FINAL_LINE,
) -> tuple[str, tuple[ReviewFinding, ...]]:
    """Validate one reviewer report and return ``(verdict, findings)``.

    A fenced ``review``/``json`` block is authoritative when present; a
    malformed block fails instead of silently falling back. Otherwise the
    canonical explicit verdict markers are required. ``verdict_source``
    selects how the text marker is located: :data:`VERDICT_SOURCE_FINAL_LINE`
    (default) trusts only the last explicit verdict line, which lets external
    engines quote both markers in their report; :data:`VERDICT_SOURCE_ANYWHERE`
    preserves the historical whole-report ambiguity check used by the Pi
    backend. An absent or ambiguous verdict, an ``approved`` verdict with
    critical findings, and a ``changes_requested`` verdict without any
    evidence-linked finding are all typed :class:`ReviewFormatError` failures.
    """
    if verdict_source not in _VERDICT_SOURCES:
        raise ValueError(f"unknown verdict source: {verdict_source!r}")
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
        if verdict_source == VERDICT_SOURCE_FINAL_LINE:
            verdict = _verdict_from_final_line(text)
        else:
            verdict = _verdict_from_markers(text)
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


def _review_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReviewFormatError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ReviewFormatError(f"invalid JSON constant: {value}")


def parse_review_json(text: str) -> tuple[str, tuple[ReviewFinding, ...], str]:
    """Validate the complete machine result; never infer a verdict from prose."""
    try:
        payload = json.loads(
            text, object_pairs_hook=_review_json_object,
            parse_constant=_reject_json_constant,
        )
    except ValueError as error:
        raise ReviewFormatError(
            f"review result must be a JSON object: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ReviewFormatError("review result must be a JSON object")
    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or verdict not in REVIEW_VERDICTS:
        raise ReviewFormatError("verdict must be approved or changes_requested")
    items = payload.get("findings")
    if not isinstance(items, list):
        raise ReviewFormatError("findings must be an array")
    findings = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ReviewFormatError(f"findings[{index}] must be an object")
        for key in ("severity", "file", "message"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ReviewFormatError(
                    f"findings[{index}].{key} must be a non-empty string"
                )
        if item["severity"] not in {"P1", "P2", "P3"}:
            raise ReviewFormatError(
                f"findings[{index}].severity must be P1, P2 or P3"
            )
        if type(item.get("line")) is not int or item["line"] < 1:
            raise ReviewFormatError(
                f"findings[{index}].line must be a positive integer"
            )
        if "evidence" in item and not isinstance(item["evidence"], str):
            raise ReviewFormatError(f"findings[{index}].evidence must be a string")
        findings.append(
            ReviewFinding(
                severity=item["severity"], file=item["file"], line=item["line"],
                message=item["message"], evidence=item.get("evidence"),
            )
        )
    if verdict == VERDICT_APPROVED and any(f.severity == "P1" for f in findings):
        raise ReviewFormatError("approved result cannot contain P1 findings")
    if verdict == VERDICT_CHANGES_REQUESTED and not findings:
        raise ReviewFormatError("changes_requested requires at least one finding")
    if "report" in payload and not isinstance(payload["report"], str):
        raise ReviewFormatError("report must be a string")
    details = "\n".join(
        f"{f.severity}: {f.file}:{f.line} — {f.message}" for f in findings
    )
    verdict_line = (
        "ВЕРДИКТ: ОДОБРЕНО" if verdict == VERDICT_APPROVED
        else "ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ"
    )
    rendered = "\n\n".join(
        part for part in (payload.get("report", ""), details, verdict_line)
        if part
    )
    return verdict, tuple(findings), rendered


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
            + REVIEW_JSON_INSTRUCTIONS
        )

    # -- execution -------------------------------------------------------

    def execute(self, request: ReviewRequest) -> ReviewResult:
        """Run and validate one review round, persisting its artifacts.

        The configured ``runner_config.engine`` selects the backend:
        :class:`PiReviewerBackend` (default), :class:`AgyReviewerBackend` or
        :class:`CodexReviewerBackend`. Every backend feeds the same strict
        report validation, so the verdict contract never depends on the tool.

        Raises:
            ReviewExecutionError: for a missing engine binary, a nonzero exit,
                failed Runner status, empty/malformed output, an absent or
                invalid verdict or any missing report. The diagnostics are
                persisted to disk first.
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
            (
                request.reviewer_system_prompt + REVIEW_JSON_INSTRUCTIONS
                if request.reviewer_system_prompt
                else DEFAULT_REVIEWER_PROMPT
            ),
            encoding="utf-8",
        )
        prompt_text = self._build_review_prompt(request)
        task_prompt_path = artifact_dir / f"review_prompt_r{round_number}.md"
        task_prompt_path.write_text(prompt_text, encoding="utf-8")

        engine = request.engine
        role_path = artifact_dir / f"role_r{round_number}.json"
        runtime_payload: dict[str, Any] = {"engine": engine}
        if engine == "pi":
            # Only the Pi Runner needs the read-only role document and the
            # provider/model runtime selection; native engines are standalone.
            role_config = self._build_review_role_config(
                request, state_dir=state_dir
            )
            runtime_selection = self._runtime_defaults.resolve(role_config)
            role_config = runtime_selection.role_config
            role_config = _build_role_config(
                role_config,
                agent_id=REVIEWER_AGENT_ID,
                prompt_path=system_prompt_path,
            )
            discovery_path = write_handbook_discovery(
                artifact_dir,
                agent_id=REVIEWER_AGENT_ID,
                tools=list(role_config.get("tools") or []),
            )
            if discovery_path is not None:
                instructions = list(role_config.get("instructions") or [])
                instructions.append(str(discovery_path))
                role_config["instructions"] = instructions
            role_path.write_text(
                json.dumps(role_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_payload.update(
                {
                    "provider": runtime_selection.provider,
                    "model": runtime_selection.model,
                    "pi_auth_provider": runtime_selection.pi_auth_provider,
                    "models_file": runtime_selection.models_file,
                    "selection": runtime_selection.selection_dict(),
                }
            )

        context = _ReviewerRunContext(
            request=request,
            round_number=round_number,
            review_artifact_dir=review_artifact_dir,
            task_prompt_path=task_prompt_path,
            prompt_text=prompt_text,
            role_path=role_path,
            runtime_payload=runtime_payload,
        )

        backend = self._backend_for(engine)
        if backend is None:
            duration_ms = (time.perf_counter() - started) * 1000.0
            raise self._failure(
                request,
                round_number,
                review_artifact_dir,
                reason="unknown_reviewer_engine",
                message=f"unknown reviewer engine: {engine!r}",
                duration_ms=duration_ms,
                runtime=runtime_payload,
            )

        # A fresh directory prevents an old result from satisfying a new run.
        execution_dir = Path(
            tempfile.mkdtemp(prefix=f"round_{round_number}_", dir=review_artifact_dir)
        )
        feedback = ""
        for attempt in range(1, MAX_FORMAT_ATTEMPTS + 1):
            attempt_dir = execution_dir / f"attempt_{attempt}"
            attempt_dir.mkdir()
            attempt_prompt = prompt_text + feedback
            attempt_prompt_path = attempt_dir / "prompt.md"
            attempt_prompt_path.write_text(attempt_prompt, encoding="utf-8")
            attempt_context = dataclasses.replace(
                context,
                review_artifact_dir=attempt_dir,
                prompt_text=attempt_prompt,
                task_prompt_path=attempt_prompt_path,
            )
            outcome = None
            reason = "invalid_report"
            try:
                outcome = backend.run(attempt_context)
                for name, content in (
                    ("stdout.txt", outcome.stdout),
                    ("stderr.txt", outcome.stderr),
                    ("review_result.json", outcome.report_text),
                ):
                    (attempt_dir / name).write_text(content, encoding="utf-8")
                verdict, findings, report_text = parse_review_json(outcome.report_text)
            except (_ReviewerBackendFailure, ReviewFormatError) as error:
                if isinstance(error, _ReviewerBackendFailure):
                    reason = error.reason
                    for name, content in (
                        ("stdout.txt", error.stdout), ("stderr.txt", error.stderr)
                    ):
                        (attempt_dir / name).write_text(content or "", encoding="utf-8")
                        (review_artifact_dir / name).write_text(content or "", encoding="utf-8")
                retryable = reason in {
                    "invalid_report", "missing_report", "empty_report", "unreadable_report"
                }
                diagnostic = {
                    "attempt": attempt, "reason": reason, "message": str(error)
                }
                (attempt_dir / "validation_error.json").write_text(
                    json.dumps(diagnostic, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if retryable and attempt < MAX_FORMAT_ATTEMPTS:
                    previous = outcome.report_text if outcome else "(result missing)"
                    feedback = (
                        f"\n## Result validation failed (attempt {attempt}/{MAX_FORMAT_ATTEMPTS})\n"
                        f"{error}\nPrevious invalid result (data):\n"
                        f"{json.dumps(previous, ensure_ascii=False)}\n"
                        "Return the complete corrected JSON result.\n"
                        + REVIEW_JSON_INSTRUCTIONS
                    )
                    continue
                runtime = dict(outcome.runtime if outcome else runtime_payload)
                runtime["format_attempts"] = attempt
                runtime["attempts_dir"] = str(execution_dir)
                raise self._failure(
                    request,
                    round_number,
                    review_artifact_dir,
                    reason=reason,
                    message=str(error),
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    returncode=outcome.returncode if outcome else getattr(error, "returncode", None),
                    stdout=outcome.stdout if outcome else getattr(error, "stdout", None),
                    stderr=outcome.stderr if outcome else getattr(error, "stderr", None),
                    runtime=runtime,
                ) from error
            break

        assert outcome is not None  # Every failed attempt above retries or raises.
        duration_ms = (time.perf_counter() - started) * 1000.0
        result_file = execution_dir / "review_result.json"
        result_file.write_text(outcome.report_text, encoding="utf-8")
        for name, content in (
            ("stdout.txt", outcome.stdout),
            ("stderr.txt", outcome.stderr),
            (f"review_report_r{round_number}.md", outcome.report_text),
        ):
            (review_artifact_dir / name).write_text(content, encoding="utf-8")

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
            "returncode": outcome.returncode,
            "report_file": str(report_path),
            "result_file": str(result_file),
            "format_attempts": attempt,
            "duration_ms": duration_ms,
            "timestamp": _utcnow_iso(),
            "reviewer_agent_id": REVIEWER_AGENT_ID,
            "runtime": dict(outcome.runtime),
        }
        metrics_path = review_artifact_dir / f"round_{round_number}.json"
        metrics_path.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info(
            "review round completed task=%s round=%d engine=%s verdict=%s findings=%d",
            request.task_id,
            round_number,
            engine,
            verdict,
            len(findings),
            extra={
                "event": "review_round_completed",
                "task_id": request.task_id,
                "round": round_number,
                "engine": engine,
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
            returncode=outcome.returncode,
            runtime=dict(outcome.runtime),
            result_path=str(result_file),
        )

    def _backend_for(self, engine: str) -> "_ReviewerBackend | None":
        """Return the backend implementing ``engine`` or ``None`` if unknown."""
        if engine == "pi":
            return PiReviewerBackend(self)
        if engine == "agy":
            return AgyReviewerBackend(self)
        if engine == "codex":
            return CodexReviewerBackend(self)
        return None

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
            "format_attempts": (runtime or {}).get("format_attempts", 0),
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


class _ReviewerBackend:
    """Base class for one concrete reviewer execution engine."""

    engine = ""

    def __init__(self, executor: ReviewExecutor) -> None:
        self._executor = executor

    def run(self, context: _ReviewerRunContext) -> _ReviewerBackendResult:
        raise NotImplementedError


class PiReviewerBackend(_ReviewerBackend):
    """Run the reviewer through the standard Pi Runner profile contract."""

    engine = "pi"

    def run(self, context: _ReviewerRunContext) -> _ReviewerBackendResult:
        request = context.request
        cmd = [
            *self._executor._runner_argv_prefix,
            "run",
            "--agent",
            str(context.role_path),
            "--task",
            str(context.task_prompt_path),
        ]
        try:
            proc = self._executor._invoke(
                cmd,
                cwd=request.project_dir,
                timeout=request.timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - surfaced as diagnostics
            raise _ReviewerBackendFailure(
                "runner_launch_failed",
                f"failed to launch reviewer: {type(error).__name__}: {error}",
                cause=error,
            ) from error

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            raise _ReviewerBackendFailure(
                "runner_nonzero_exit",
                f"reviewer process exited with code {proc.returncode}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        payload = _extract_last_json_object(stdout)
        if payload is None:
            raise _ReviewerBackendFailure(
                "malformed_runner_output",
                "reviewer produced no parseable JSON result object",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        status = payload.get("status")
        if status != "completed":
            raise _ReviewerBackendFailure(
                "runner_status_not_completed",
                f"reviewer reported status {status!r}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        summary_path_raw = payload.get("summary_path")
        summary_path = Path(summary_path_raw) if summary_path_raw else None
        if summary_path is None or not summary_path.is_file():
            raise _ReviewerBackendFailure(
                "missing_report",
                "reviewer completed without a readable report file: "
                f"{summary_path_raw!r}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        try:
            report_text = summary_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise _ReviewerBackendFailure(
                "unreadable_report",
                f"cannot read reviewer report: {type(error).__name__}: {error}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                cause=error,
            ) from error

        if not report_text:
            raise _ReviewerBackendFailure(
                "empty_report",
                "reviewer report is empty",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        return _ReviewerBackendResult(
            report_text=report_text,
            returncode=proc.returncode,
            runtime=context.runtime_payload,
            stdout=stdout,
            stderr=stderr,
        )


class _ExternalReviewerBackend(_ReviewerBackend):
    """Common contract for reviewer CLIs that report their result.

    The default transport is stdout; an engine whose CLI owns its output
    format (codex) overrides :meth:`_read_report` to read a result file the
    engine writes at an exact path instead.
    """

    def _read_report(
        self, context: _ReviewerRunContext, stdout: str
    ) -> str:
        """Return the report text produced by one engine run."""
        return stdout.strip()

    def _build_command(
        self,
        binary: Path,
        prompt: str,
        context: _ReviewerRunContext,
    ) -> list[str]:
        raise NotImplementedError

    def _prompt_with_system_instructions(
        self, context: _ReviewerRunContext
    ) -> str:
        """Prepend the stored reviewer system prompt for native engines.

        Native engines receive the task prompt directly on the command line and
        have no role document, so the knowledge-base reviewer prompt must be
        embedded into that prompt to keep custom instructions effective.
        """
        system_prompt = (context.request.reviewer_system_prompt or "").strip()
        if not system_prompt:
            return context.prompt_text
        return (
            "<SYSTEM INSTRUCTIONS>\n"
            f"{system_prompt}\n"
            "</SYSTEM INSTRUCTIONS>\n\n"
            f"{context.prompt_text}"
        )

    def _runtime_details(
        self, context: _ReviewerRunContext
    ) -> Mapping[str, Any]:
        return {}

    def _binary_not_found(self) -> _ReviewerBackendFailure:
        return _ReviewerBackendFailure(
            "engine_binary_not_found",
            f"Reviewer engine '{self.engine}' binary not found",
        )

    def run(self, context: _ReviewerRunContext) -> _ReviewerBackendResult:
        binary = find_reviewer_binary(self.engine)
        if binary is None:
            raise self._binary_not_found()

        request = context.request
        cmd = self._build_command(
            binary, self._prompt_with_system_instructions(context), context
        )
        # Request-level wins; then the reviewer profile's runner_config
        # ``timeout_seconds``; the default must fit a real review, which
        # easily runs several minutes on an external engine.
        configured = (request.reviewer_runner_config or {}).get("timeout_seconds")
        if request.timeout_seconds is not None:
            timeout = request.timeout_seconds
        elif isinstance(configured, (int, float)) and configured > 0:
            timeout = float(configured)
        else:
            timeout = DEFAULT_EXTERNAL_REVIEW_TIMEOUT_SECONDS
        try:
            proc = self._executor._invoke(
                cmd,
                cwd=request.project_dir,
                timeout=timeout,
            )
        except Exception as error:  # noqa: BLE001 - surfaced as diagnostics
            raise _ReviewerBackendFailure(
                "reviewer_launch_failed",
                f"failed to launch reviewer engine '{self.engine}': "
                f"{type(error).__name__}: {error}",
                cause=error,
            ) from error

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        (context.review_artifact_dir / "stdout.txt").write_text(
            stdout, encoding="utf-8"
        )
        (context.review_artifact_dir / "stderr.txt").write_text(
            stderr, encoding="utf-8"
        )

        if proc.returncode != 0:
            raise _ReviewerBackendFailure(
                "reviewer_nonzero_exit",
                f"reviewer engine '{self.engine}' exited with code "
                f"{proc.returncode}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        # Read after the exit check: a crashed engine reports its exit code,
        # not a missing result file.
        report_text = self._read_report(context, stdout)
        report_path = (
            context.review_artifact_dir
            / f"review_report_r{context.round_number}.md"
        )
        report_path.write_text(report_text, encoding="utf-8")

        if not report_text:
            raise _ReviewerBackendFailure(
                "empty_report",
                f"reviewer engine '{self.engine}' produced an empty report",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        runtime = dict(context.runtime_payload)
        runtime.update(
            {
                "engine": self.engine,
                "binary": str(binary),
                "timeout_seconds": timeout,
            }
        )
        runtime.update(self._runtime_details(context))
        return _ReviewerBackendResult(
            report_text=report_text,
            returncode=proc.returncode,
            runtime=runtime,
            stdout=stdout,
            stderr=stderr,
        )


class _FileResultContractMixin:
    """File-based result delivery for reviewer CLIs that own their stdout.

    The engine gets an exact per-attempt result path in its prompt and writes
    the JSON contract there; the platform reads the file and ignores stdout
    (kept only as a diagnostic artifact). A stale file never counts: the
    path is wiped before every launch and each format attempt runs in its
    own directory.
    """

    engine: str

    def _result_path(self, context: _ReviewerRunContext) -> Path:
        return (
            context.review_artifact_dir
            / f"engine_result_r{context.round_number}.json"
        )

    def _read_report(self, context: _ReviewerRunContext, stdout: str) -> str:
        path = self._result_path(context)
        if not path.is_file():
            raise _ReviewerBackendFailure(
                "missing_report",
                f"{self.engine} did not write the result file {path}",
                stdout=stdout,
            )
        return path.read_text(encoding="utf-8").strip()

    def _result_directive(self, context: _ReviewerRunContext) -> str:
        result_path = self._result_path(context)
        result_path.unlink(missing_ok=True)
        return (
            f"\n\nWrite the final JSON object to the file {result_path} "
            "(create or overwrite it; the file must contain only that JSON, "
            "no Markdown fences). Do not create or modify any other files."
        )


class AgyReviewerBackend(
    _FileResultContractMixin, _ExternalReviewerBackend
):
    """Run ``agy`` in auto mode with the file-based result contract.

    ``--mode plan`` is read-only by design and cannot deliver a result file,
    and agy formats its own stdout presentation, so the engine runs in
    ``auto`` mode with permissions skipped (the reviewer runs on the host,
    like the codex engine) and writes the JSON to an exact file path.

    ``--model`` and ``--effort`` are passed only when explicitly configured
    in the reviewer ``runner_config``. agy's default model (from
    ``~/.pi/agent/settings.json``) supports no effort levels, and most models
    reject ``--effort`` outright — an implicit ``--effort high`` made every
    run exit 1 with "invalid model selection". With no explicit config the
    command relies on agy's own defaults, which always run.
    """

    engine = "agy"

    @staticmethod
    def _model(context: _ReviewerRunContext) -> str | None:
        runner_config = context.request.reviewer_runner_config or {}
        if not isinstance(runner_config, Mapping):
            return None
        requested = runner_config.get("model")
        return str(requested) if requested else None

    @staticmethod
    def _effort(context: _ReviewerRunContext) -> str | None:
        runner_config = context.request.reviewer_runner_config or {}
        if not isinstance(runner_config, Mapping):
            return None
        requested = runner_config.get("effort")
        return str(requested) if requested else None

    def _runtime_details(
        self, context: _ReviewerRunContext
    ) -> Mapping[str, Any]:
        details: dict[str, Any] = {}
        model = self._model(context)
        effort = self._effort(context)
        if model is not None:
            details["model"] = model
        if effort is not None:
            details["effort"] = effort
        return details

    def _build_command(
        self,
        binary: Path,
        prompt: str,
        context: _ReviewerRunContext,
    ) -> list[str]:
        command = [str(binary), "--mode", "auto", "--dangerously-skip-permissions"]
        model = self._model(context)
        if model is not None:
            command += ["--model", model]
        effort = self._effort(context)
        if effort is not None:
            command += ["--effort", effort]
        command += ["-p", prompt + self._result_directive(context)]
        return command


class CodexReviewerBackend(
    _FileResultContractMixin, _ExternalReviewerBackend
):
    """Run ``codex exec`` with the file-based result contract.

    ``codex review`` formats its own human-readable report and rejects a
    custom prompt combined with scope flags, so the JSON contract cannot be
    delivered over stdout there. Instead ``codex exec`` runs the review as a
    plain agent turn: the prompt carries the rubric plus the exact result
    file path, and the platform reads the file, ignoring stdout.
    """

    engine = "codex"

    def _build_command(
        self,
        binary: Path,
        prompt: str,
        context: _ReviewerRunContext,
    ) -> list[str]:
        return [
            str(binary),
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt + self._result_directive(context),
        ]
