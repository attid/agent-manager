"""Evidence-backed task analysis and approval-gated recommendations.

This module is the durable ``analysis`` executor behind
``trigger_analysis``. It is deliberately narrow:

* :func:`build_evidence_package` reads only data related to the target task
  (its own fields, the selected executor profile revision, operations that
  target the task, available review evidence and metrics). It never scans or
  serializes unrelated tasks, conversations or profiles.
* :func:`analysis_operation_handler` runs the pinned ``system-log-analyzer``
  profile through an injectable analyzer runner, parses a strict structured
  document and persists a report plus evidence-linked recommendations.
* :class:`RecommendationService` exposes the preview/approve/apply/status
  workflow. Proposal generation never mutates a target; applying an approved
  recommendation re-checks the observed revision or content hash and surfaces
  a stale conflict instead of overwriting newer content.

The analyzer itself has no mutation path: it receives a read-only evidence
document and returns text. Nothing here applies a recommendation on the
analyzer's behalf, and the executor never calls the apply service.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .capabilities import write_handbook_discovery
from .knowledge import KnowledgeBase, KnowledgePathError, contained_target_path
from .operation_worker import (
    OperationContext,
    OperationExecutionError,
    OperationInstanceContext,
)
from .storage import (
    RECOMMENDATION_STATUS_APPLIED,
    RECOMMENDATION_STATUS_APPROVED,
    AgentProfileRevisionConflictError,
    AgentProfileRepository,
    AnalysisRepository,
    Database,
    OperationRepository,
    Recommendation,
    RecommendationNotFoundError,
    RecommendationStateError,
    Task,
    TaskRepository,
)

logger = logging.getLogger(__name__)

#: Core analyzer agent that owns session analysis.
ANALYZER_AGENT_ID = "system-log-analyzer"

#: The analyzer profile is strictly read-only; it never receives a tool that
#: can mutate code, profiles or the knowledge base. Discovery is filtered
#: against this list so no platform action is advertised as executable.
ANALYZER_READ_ONLY_TOOLS = ("read", "bash")

#: Recommendation target kinds. A profile target is a versioned agent profile;
#: project/component targets are knowledge-base markdown files.
TARGET_KIND_PROFILE = "profile"
TARGET_KIND_PROJECT = "project"
TARGET_KIND_COMPONENT = "component"
TARGET_KINDS = frozenset(
    {TARGET_KIND_PROFILE, TARGET_KIND_PROJECT, TARGET_KIND_COMPONENT}
)

#: Analyzer output contract appended to every run. The analyzer must return a
#: single JSON object; anything else is a visible failure, never a fake pass.
ANALYZER_OUTPUT_CONTRACT = """\
Return exactly one JSON object and no prose outside it. Shape:

{
  "summary": "<non-empty factual summary of the analyzed task session>",
  "findings": [
    {"title": "<short>", "detail": "<observed fact vs hypothesis>",
     "evidence": ["<evidence id, e.g. task:T1 or operation:<id>>"]}
  ],
  "recommendations": [
    {
      "target_kind": "profile" | "project" | "component",
      "target_id": "<agent id, project name or component name>",
      "title": "<short proposal title>",
      "rationale": "<why this change is warranted>",
      "evidence": ["<evidence id>"],
      "proposed_content": "<full proposed text, or an object of profile fields>",
      "target_revision": <integer profile revision, or null>,
      "target_content_hash": "<sha256 of the observed file content, or null>",
      "diff": "<optional unified diff; the server computes one when omitted>"
    }
  ]
}

`recommendations` may be an empty list when no change is warranted. Every
recommendation must name the exact observed target so a later apply can
detect stale content.
"""


class AnalysisError(Exception):
    """Base class for analysis domain failures."""


class AnalysisParseError(AnalysisError):
    """Raised when analyzer output is missing or not the agreed JSON shape."""


class RecommendationConflictError(AnalysisError):
    """Raised when applying an approved recommendation finds a stale target.

    The recommendation is persisted ``conflict`` first; the target is never
    overwritten.
    """

    def __init__(self, recommendation_id: str, reason: str) -> None:
        super().__init__(
            f"recommendation {recommendation_id} conflicts with the current "
            f"target: {reason}"
        )
        self.recommendation_id = recommendation_id
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class AnalysisRequest:
    """Read-only input handed to one analyzer runner invocation."""

    operation_id: str
    task_id: str
    evidence: Mapping[str, Any]
    instructions: str
    instance: OperationInstanceContext


@dataclasses.dataclass(frozen=True)
class AnalyzerRunResult:
    """Text plus provenance returned by one analyzer runner invocation."""

    text: str
    agent_id: str = ANALYZER_AGENT_ID
    profile_revision: int | None = None
    session_id: str | None = None
    provenance: Mapping[str, Any] | None = None


#: An analyzer runner maps one request to its structured text output.
AnalyzerRunner = Callable[[AnalysisRequest], AnalyzerRunResult]


@dataclasses.dataclass(frozen=True)
class RecommendationPreview:
    """Current target state and whether the proposal still applies cleanly."""

    recommendation: Recommendation
    target_exists: bool
    current_revision: int | None
    current_content_hash: str | None
    current_content: str | None
    would_conflict: bool
    conflict_reason: str | None


# -- helpers -------------------------------------------------------------


def content_hash(text: str) -> str:
    """Return the lowercase SHA-256 hex digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    """Convert ORM/date/path values into JSON-serializable ones."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _extract_json_object(text: str) -> str | None:
    """Extract the first balanced JSON object from ``text``.

    A fenced `````json`` block is preferred; otherwise the first balanced
    object in the text is used. Brace matching is depth-aware and ignores
    braces inside JSON strings.
    """
    if not isinstance(text, str):
        return None
    fence_start = text.find("```")
    if fence_start != -1:
        after = text[fence_start + 3 :]
        if after.lower().startswith("json"):
            after = after[4:]
        fence_end = after.find("```")
        if fence_end != -1:
            candidate = after[:fence_end].strip()
            if candidate.startswith("{"):
                return candidate
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_analyzer_output(text: str) -> dict[str, Any]:
    """Parse and validate the analyzer's structured report document.

    Raises:
        AnalysisParseError: when no JSON object is present, it is not valid
            JSON, has no non-empty ``summary``, has a non-list
            ``recommendations`` value or a malformed recommendation.
    """
    if not isinstance(text, str) or not text.strip():
        raise AnalysisParseError("analyzer returned an empty report")
    candidate = _extract_json_object(text)
    if candidate is None:
        raise AnalysisParseError("analyzer did not return a JSON object")
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise AnalysisParseError(f"analyzer returned invalid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise AnalysisParseError("analyzer JSON must be an object")
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AnalysisParseError("analyzer report has no non-empty summary")
    findings = parsed.get("findings", [])
    if not isinstance(findings, list):
        raise AnalysisParseError("analyzer findings must be a list")
    recommendations = parsed.get("recommendations", [])
    if not isinstance(recommendations, list):
        raise AnalysisParseError("analyzer recommendations must be a list")
    for index, recommendation in enumerate(recommendations):
        _validate_recommendation(recommendation, index)
    return parsed


def _validate_recommendation(recommendation: Any, index: int) -> None:
    label = f"recommendations[{index}]"
    if not isinstance(recommendation, Mapping):
        raise AnalysisParseError(f"{label} must be an object")
    target_kind = recommendation.get("target_kind")
    if target_kind not in TARGET_KINDS:
        raise AnalysisParseError(
            f"{label}.target_kind must be one of {sorted(TARGET_KINDS)}"
        )
    target_id = recommendation.get("target_id")
    if not isinstance(target_id, str) or not target_id.strip():
        raise AnalysisParseError(f"{label}.target_id must be a non-empty string")
    for field_name in ("title", "rationale"):
        value = recommendation.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise AnalysisParseError(
                f"{label}.{field_name} must be a non-empty string"
            )
    if "proposed_content" not in recommendation:
        raise AnalysisParseError(f"{label}.proposed_content is required")
    evidence = recommendation.get("evidence", [])
    if not isinstance(evidence, list):
        raise AnalysisParseError(f"{label}.evidence must be a list")


# -- evidence package ----------------------------------------------------


def build_evidence_package(database: Database, task: Task) -> dict[str, Any]:
    """Build a bounded-by-relevance, read-only evidence package for ``task``.

    Only data related to this task is included: the task snapshot, the
    selected executor profile revision, operations whose ``target_id`` is the
    task, available review evidence and metrics. No other task, conversation
    or profile is read.
    """
    operations = OperationRepository(database).list(target_id=task.id)
    profiles = AgentProfileRepository(database)
    profile = (
        profiles.get_current(task.agent_id) if task.agent_id else None
    )

    items: list[dict[str, Any]] = []

    def add(item_id: str, kind: str, content: Any) -> None:
        items.append(
            {"id": item_id, "kind": kind, "content": _jsonable(content)}
        )

    meta = dict(task.meta or {})
    add(
        f"task:{task.id}",
        "task",
        {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "agent_id": task.agent_id,
            "workspace": task.workspace,
            "metadata": meta,
        },
    )
    if profile is not None:
        add(
            f"profile:{profile.agent_id}@{profile.revision}",
            "profile",
            {
                "agent_id": profile.agent_id,
                "revision": profile.revision,
                "name": profile.name,
                "role": profile.role,
                "system_prompt": profile.system_prompt,
                "runner_config": profile.runner_config,
            },
        )

    operation_items: list[dict[str, Any]] = []
    for operation in operations:
        entry = {
            "id": operation.id,
            "kind": operation.kind,
            "status": operation.status,
            "target_id": operation.target_id,
            "input": operation.input or {},
            "result": operation.result,
            "error": operation.error,
            "attempt_count": operation.attempt_count,
            "created_at": _jsonable(operation.created_at),
            "claimed_at": _jsonable(operation.claimed_at),
            "finished_at": _jsonable(operation.finished_at),
            "metadata": operation.meta or {},
        }
        operation_items.append(entry)
        add(f"operation:{operation.id}", "operation", entry)

    reviews = _collect_review_evidence(task, operations)
    for review in reviews:
        add(review["id"], "review", review)

    metrics = _collect_metrics(task, operations)
    add("metrics:task", "metrics", metrics)

    return {
        "task_id": task.id,
        "task": {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "agent_id": task.agent_id,
            "workspace": task.workspace,
            "metadata": meta,
        },
        "profile": (
            {
                "agent_id": profile.agent_id,
                "revision": profile.revision,
                "name": profile.name,
                "role": profile.role,
            }
            if profile is not None
            else None
        ),
        "operations": operation_items,
        "reviews": reviews,
        "metrics": metrics,
        "items": items,
    }


def _collect_review_evidence(
    task: Task, operations: Sequence[Any]
) -> list[dict[str, Any]]:
    """Return review evidence already attached to ``task``.

    The default provider reads only data reachable from the task: explicit
    review entries in task metadata and review operations that target the
    task. A later review integration can supply richer evidence without this
    module importing its internals.
    """
    reviews: list[dict[str, Any]] = []
    meta = dict(task.meta or {})
    for key in ("review", "reviews"):
        value = meta.get(key)
        if value is None:
            continue
        entries = value if isinstance(value, list) else [value]
        for index, entry in enumerate(entries):
            reviews.append(
                {
                    "id": f"review:metadata:{task.id}:{key}:{index}",
                    "source": "task_metadata",
                    "key": key,
                    "content": _jsonable(entry),
                }
            )
    for operation in operations:
        if operation.kind != "review":
            continue
        result = operation.result if isinstance(operation.result, Mapping) else None
        reviews.append(
            {
                "id": f"review:operation:{operation.id}",
                "source": "operation",
                "status": operation.status,
                "verdict": result.get("verdict") if result else None,
                "report": _jsonable(operation.result),
                "error": operation.error,
            }
        )
    return reviews


def _collect_metrics(
    task: Task, operations: Sequence[Any]
) -> dict[str, Any]:
    """Return metrics reachable from the task and its operations."""
    meta = dict(task.meta or {})
    metrics: dict[str, Any] = {}
    if isinstance(meta.get("metrics"), Mapping):
        metrics.update(_jsonable(meta["metrics"]))
    for key in ("usage", "validation", "validation_report"):
        if key in meta:
            metrics[key] = _jsonable(meta[key])
    metrics["operation_count"] = len(operations)
    metrics["operation_statuses"] = {
        operation.status: sum(
            1 for item in operations if item.status == operation.status
        )
        for operation in operations
    }
    return metrics


# -- analyzer runner seam ------------------------------------------------


def default_analyzer_runner(instance: OperationInstanceContext) -> AnalyzerRunner:
    """Build the production runner that launches ``system-log-analyzer``.

    The runner freezes the current analyzer profile revision for exactly this
    execution, runs it through the existing Pi reply handler and returns its
    summary text. It mutates nothing; the analyzer profile is mounted
    read-only by the Runner.
    """

    def run(request: AnalysisRequest) -> AnalyzerRunResult:
        # Imported lazily so tests that inject a runner never pay the Pi
        # adapter import cost and so no import cycle forms at module load.
        from .profile_runtime import freeze_profile_handler
        from .secretary import TurnInput

        artifact_root = Path(instance.artifact_root) / "analysis"
        discovery_dir = artifact_root / f"discovery_{request.operation_id}"
        discovery_dir.mkdir(parents=True, exist_ok=True)
        discovery_path = write_handbook_discovery(
            discovery_dir,
            agent_id=ANALYZER_AGENT_ID,
            tools=list(ANALYZER_READ_ONLY_TOOLS),
        )
        frozen = freeze_profile_handler(
            instance.database,
            ANALYZER_AGENT_ID,
            runner_argv_prefix=list(instance.runner_argv_prefix),
            artifact_root=artifact_root,
            runtime_defaults=getattr(instance, "runtime_defaults", None),
            extra_instructions=(
                [str(discovery_path)] if discovery_path is not None else None
            ),
        )
        turn = TurnInput(
            turn_id=f"analysis:{request.operation_id}",
            conversation_id=f"analysis:{request.task_id}",
            input_message_id=f"analysis:{request.operation_id}",
            role="user",
            text=request.instructions,
            metadata={},
            attempt_count=1,
        )
        text = frozen(turn)
        provenance = frozen.last_execution
        return AnalyzerRunResult(
            text=text,
            agent_id=frozen.agent_id,
            profile_revision=frozen.revision,
            session_id=(
                provenance.returned_session_id if provenance else None
            ),
            provenance=provenance.to_dict() if provenance else None,
        )

    return run


def resolve_analyzer_runner(instance: OperationInstanceContext) -> AnalyzerRunner:
    """Return the injected analyzer runner or the default Pi runner."""
    injected = getattr(instance, "analyzer_runner", None)
    if injected is not None:
        return injected
    return default_analyzer_runner(instance)


def _render_instructions(
    task: Task, evidence: Mapping[str, Any]
) -> str:
    """Render the analyzer user text: evidence plus the output contract."""
    return (
        "Analyze the following task-session evidence. Use only these facts.\n\n"
        f"Task: {task.id} — {task.title}\n\n"
        "Evidence package (JSON):\n"
        f"{json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)}\n\n"
        f"{ANALYZER_OUTPUT_CONTRACT}"
    )


# -- executor ------------------------------------------------------------


def analysis_operation_handler(context: OperationContext) -> dict[str, Any]:
    """Durable ``analysis`` operation executor.

    Raises :class:`OperationExecutionError` (recorded as a failed operation)
    when the task is missing, the analyzer fails, returns nothing or returns a
    malformed document. It writes no recommendation application.
    """
    database = context.instance.database
    task_id = (context.target_id or "").strip()
    if not task_id:
        raise OperationExecutionError("analysis requires a target task id")
    task = TaskRepository(database).get(task_id)
    if task is None:
        raise OperationExecutionError(f"task not found: {task_id}")

    evidence = build_evidence_package(database, task)
    runner = resolve_analyzer_runner(context.instance)
    request = AnalysisRequest(
        operation_id=context.operation_id,
        task_id=task_id,
        evidence=evidence,
        instructions=_render_instructions(task, evidence),
        instance=context.instance,
    )
    try:
        result = runner(request)
    except OperationExecutionError:
        raise
    except Exception as error:  # noqa: BLE001 - recorded as failure
        raise OperationExecutionError(
            f"analyzer run failed: {type(error).__name__}: {error}"
        ) from error

    if not isinstance(result, AnalyzerRunResult):
        raise OperationExecutionError(
            "analyzer runner returned an invalid result"
        )
    if not result.text or not result.text.strip():
        raise OperationExecutionError("analyzer returned an empty report")

    try:
        report = parse_analyzer_output(result.text)
    except AnalysisParseError as error:
        raise OperationExecutionError(
            f"malformed analyzer report: {error}"
        ) from error

    repository = AnalysisRepository(database)
    knowledge_base = _resolve_knowledge_base(context.instance)
    stored = repository.create_report(
        task_id=task_id,
        agent_id=result.agent_id,
        profile_revision=result.profile_revision,
        summary=report.get("summary"),
        report=report,
        evidence=evidence,
    )

    recommendation_results: list[dict[str, Any]] = []
    for recommendation in report.get("recommendations", []):
        created = _persist_recommendation(
            database,
            repository,
            knowledge_base,
            report_id=stored.id,
            task_id=task_id,
            recommendation=recommendation,
        )
        recommendation_results.append(_recommendation_to_result(created))

    logger.info(
        "analysis completed",
        extra={
            "event": "analysis_completed",
            "operation_id": context.operation_id,
            "task_id": task_id,
            "report_id": stored.id,
            "recommendation_count": len(recommendation_results),
        },
    )
    return {
        "report_id": stored.id,
        "task_id": task_id,
        "agent_id": result.agent_id,
        "profile_revision": result.profile_revision,
        "session_id": result.session_id,
        "summary": report.get("summary"),
        "recommendations": recommendation_results,
    }


def _resolve_knowledge_base(
    instance: OperationInstanceContext,
) -> KnowledgeBase | None:
    knowledge_base = getattr(instance, "knowledge_base", None)
    if isinstance(knowledge_base, KnowledgeBase):
        return knowledge_base
    return None


def _persist_recommendation(
    database: Database,
    repository: AnalysisRepository,
    knowledge_base: KnowledgeBase | None,
    *,
    report_id: str,
    task_id: str,
    recommendation: Mapping[str, Any],
) -> Recommendation:
    """Persist one proposal, capturing the observed target revision/hash.

    The observed state is read from the target itself, never trusted from the
    analyzer, so a later apply can detect stale content reliably.
    """
    target_kind = str(recommendation["target_kind"])
    target_id = str(recommendation["target_id"]).strip()
    if (
        knowledge_base is not None
        and target_kind in (TARGET_KIND_PROJECT, TARGET_KIND_COMPONENT)
        and _knowledge_target_path(knowledge_base, target_kind, target_id) is None
    ):
        # Never persist a proposal whose target escapes the instance subtree:
        # preview, evidence hash and apply must all use the same contained path.
        raise OperationExecutionError(
            f"recommendation target {target_id!r} is outside the knowledge base"
        )
    revision, observed_hash, current_content = _current_target_state(
        database,
        knowledge_base,
        target_kind,
        target_id,
    )
    proposed_content = recommendation["proposed_content"]
    diff = recommendation.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        diff = _build_diff(target_kind, current_content, proposed_content)
    try:
        return repository.create_recommendation(
            report_id=report_id,
            task_id=task_id,
            target_kind=target_kind,
            target_id=target_id,
            title=str(recommendation["title"]),
            rationale=str(recommendation["rationale"]),
            evidence=list(recommendation.get("evidence", [])),
            proposed_content=proposed_content,
            diff=diff,
            target_revision=revision,
            target_content_hash=observed_hash,
        )
    except ValueError as error:
        raise OperationExecutionError(str(error)) from error


def _build_diff(
    target_kind: str, current_content: str | None, proposed_content: Any
) -> str | None:
    if target_kind == TARGET_KIND_PROFILE:
        return None
    if not isinstance(proposed_content, str):
        return None
    before = (current_content or "").splitlines(keepends=True)
    after = (proposed_content.strip() + "\n").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile="current",
            tofile="proposed",
        )
    ) or None


def _recommendation_to_result(
    recommendation: Recommendation,
) -> dict[str, Any]:
    return {
        "id": recommendation.id,
        "report_id": recommendation.report_id,
        "task_id": recommendation.task_id,
        "target_kind": recommendation.target_kind,
        "target_id": recommendation.target_id,
        "target_revision": recommendation.target_revision,
        "target_content_hash": recommendation.target_content_hash,
        "title": recommendation.title,
        "rationale": recommendation.rationale,
        "status": recommendation.status,
        "diff": recommendation.diff,
    }


# -- target state and apply service --------------------------------------


def _current_target_state(
    database: Database,
    knowledge_base: KnowledgeBase | None,
    target_kind: str,
    target_id: str,
) -> tuple[int | None, str | None, str | None]:
    """Return ``(revision, content_hash, current_content)`` for a target."""
    if target_kind == TARGET_KIND_PROFILE:
        profile = AgentProfileRepository(database).get_current(target_id)
        if profile is None:
            return None, None, None
        return profile.revision, None, None
    if knowledge_base is None:
        return None, None, None
    path = _knowledge_target_path(knowledge_base, target_kind, target_id)
    if path is None or not path.is_file():
        return None, None, None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None, None
    return None, content_hash(content), content


def _knowledge_target_path(
    knowledge_base: KnowledgeBase, target_kind: str, target_id: str
) -> Path | None:
    """Return the safe knowledge path for a target, or ``None`` when it escapes.

    Uses the same containment resolver as the final write, so preview, the
    observed evidence hash and the applied file can never disagree about
    which path a target names.
    """
    try:
        if target_kind == TARGET_KIND_PROJECT:
            return contained_target_path(
                knowledge_base.projects_dir, target_id, "summary.md"
            )
        if target_kind == TARGET_KIND_COMPONENT:
            return contained_target_path(
                knowledge_base.components_dir, target_id, "rules.md"
            )
    except KnowledgePathError as error:
        logger.warning(
            "rejected knowledge target kind=%s id=%r: %s",
            target_kind,
            target_id,
            error,
        )
        return None
    return None


class RecommendationService:
    """Preview, approve and apply evidence-linked recommendations.

    Args:
        database: The durable database that stores the recommendations.
        knowledge_base: Knowledge base used to read and write project/component
            targets. Required only for project/component recommendations.
        profile_repository: Optional repository override for profile targets.
    """

    def __init__(
        self,
        database: Database,
        *,
        knowledge_base: KnowledgeBase | None = None,
        profile_repository: AgentProfileRepository | None = None,
    ) -> None:
        self._database = database
        self._knowledge_base = knowledge_base
        self._profile_repository = (
            profile_repository
            if profile_repository is not None
            else AgentProfileRepository(database)
        )
        self._repository = AnalysisRepository(database)

    def status(self, recommendation_id: str) -> Recommendation:
        """Return the stored recommendation or raise if it is unknown."""
        recommendation = self._repository.get_recommendation(recommendation_id)
        if recommendation is None:
            raise RecommendationNotFoundError(recommendation_id)
        return recommendation

    def approve(self, recommendation_id: str) -> Recommendation:
        """Explicitly move a ``proposed`` recommendation to ``approved``."""
        return self._repository.approve_recommendation(recommendation_id)

    def preview(self, recommendation_id: str) -> RecommendationPreview:
        """Return current target state and whether the proposal is stale."""
        recommendation = self.status(recommendation_id)
        revision, observed_hash, current_content = _current_target_state(
            self._database,
            self._knowledge_base,
            recommendation.target_kind,
            recommendation.target_id,
        )
        exists = _target_exists(
            self._database,
            self._knowledge_base,
            recommendation.target_kind,
            recommendation.target_id,
        )
        conflict_reason = self._conflict_reason(
            recommendation, revision, observed_hash, exists
        )
        return RecommendationPreview(
            recommendation=recommendation,
            target_exists=exists,
            current_revision=revision,
            current_content_hash=observed_hash,
            current_content=current_content,
            would_conflict=conflict_reason is not None,
            conflict_reason=conflict_reason,
        )

    def apply(self, recommendation_id: str) -> Recommendation:
        """Apply an ``approved`` recommendation after re-checking the target.

        On a stale target the recommendation is persisted ``conflict`` and
        :class:`RecommendationConflictError` is raised; the target is never
        overwritten. On success the recommendation becomes ``applied``.
        """
        recommendation = self.status(recommendation_id)
        if recommendation.status == RECOMMENDATION_STATUS_APPLIED:
            # Idempotent: an already-applied recommendation is never re-applied.
            return recommendation
        if recommendation.status != RECOMMENDATION_STATUS_APPROVED:
            raise RecommendationStateError(
                recommendation_id, recommendation.status, "apply"
            )
        revision, observed_hash, current_content = _current_target_state(
            self._database,
            self._knowledge_base,
            recommendation.target_kind,
            recommendation.target_id,
        )
        exists = _target_exists(
            self._database,
            self._knowledge_base,
            recommendation.target_kind,
            recommendation.target_id,
        )
        conflict_reason = self._conflict_reason(
            recommendation, revision, observed_hash, exists
        )
        if conflict_reason is not None:
            self._repository.mark_conflict(
                recommendation_id,
                detail={
                    "reason": conflict_reason,
                    "observed_revision": revision,
                    "observed_content_hash": observed_hash,
                },
            )
            raise RecommendationConflictError(recommendation_id, conflict_reason)

        try:
            applied = self._apply_to_target(
                recommendation, current_content
            )
        except RecommendationConflictError as error:
            # A writer changed the target between the pre-check and the write.
            self._repository.mark_conflict(
                recommendation_id,
                detail={"reason": error.reason},
            )
            raise
        except Exception as error:  # noqa: BLE001 - persisted then re-raised
            self._repository.mark_failed(
                recommendation_id,
                detail={
                    "error": f"{type(error).__name__}: {error}",
                },
            )
            raise

        new_revision = _jsonable(applied.get("revision"))
        new_hash = applied.get("content_hash")
        detail = {
            key: _jsonable(value)
            for key, value in applied.items()
            if key not in {"revision", "content_hash"}
        }
        return self._repository.mark_applied(
            recommendation_id,
            applied_revision=new_revision,
            applied_content_hash=new_hash,
            detail=detail or None,
        )

    # -- internals -------------------------------------------------------

    def _conflict_reason(
        self,
        recommendation: Recommendation,
        revision: int | None,
        observed_hash: str | None,
        exists: bool,
    ) -> str | None:
        if recommendation.target_kind == TARGET_KIND_PROFILE:
            if not exists:
                return f"profile {recommendation.target_id!r} no longer exists"
            if revision != recommendation.target_revision:
                return (
                    f"profile {recommendation.target_id!r} is now revision "
                    f"{revision}, expected {recommendation.target_revision}"
                )
            return None
        if not exists:
            if recommendation.target_content_hash is not None:
                return (
                    f"{recommendation.target_kind} {recommendation.target_id!r} "
                    "no longer exists"
                )
            return None
        if observed_hash != recommendation.target_content_hash:
            return (
                f"{recommendation.target_kind} "
                f"{recommendation.target_id!r} content hash changed"
            )
        return None

    def _apply_to_target(
        self, recommendation: Recommendation, current_content: str | None
    ) -> dict[str, Any]:
        if recommendation.target_kind == TARGET_KIND_PROFILE:
            return self._apply_profile(recommendation)
        return self._apply_knowledge(recommendation, current_content)

    def _apply_profile(self, recommendation: Recommendation) -> dict[str, Any]:
        proposed = recommendation.proposed_content
        if isinstance(proposed, str):
            # The analyzer output contract permits the full proposed text for a
            # profile recommendation. Treat it as the replacement system
            # prompt instead of rejecting a document the contract allowed.
            if not proposed.strip():
                raise AnalysisError(
                    "profile recommendation proposed_content must be a "
                    "non-empty string or an object"
                )
            proposed = {"system_prompt": proposed}
        if not isinstance(proposed, Mapping):
            raise AnalysisError(
                "profile recommendation proposed_content must be an object or "
                "a non-empty system prompt string"
            )
        system_prompt = proposed.get("system_prompt")
        if system_prompt is not None and (
            not isinstance(system_prompt, str) or not system_prompt.strip()
        ):
            raise AnalysisError(
                "profile recommendation system_prompt must be a non-empty string"
            )
        allowed = {
            "name",
            "role",
            "system_prompt",
            "runner_config",
            "skills",
            "knowledge",
        }
        # The analyzer commonly echoes the observed profile identity and
        # revision into the proposal. Those fields are server-managed: the
        # target is fixed by ``target_id`` and the apply re-checks
        # ``target_revision``, so ignore them instead of failing the apply.
        server_managed = {"agent_id", "revision"}
        replacements = {
            key: value for key, value in proposed.items() if key in allowed
        }
        unknown = set(proposed) - allowed - server_managed
        if unknown:
            raise AnalysisError(
                f"profile recommendation has unsupported fields: "
                f"{sorted(unknown)}"
            )
        assert recommendation.target_revision is not None
        try:
            updated = self._profile_repository.update(
                agent_id=recommendation.target_id,
                expected_revision=recommendation.target_revision,
                **replacements,
            )
        except AgentProfileRevisionConflictError as error:
            raise RecommendationConflictError(
                recommendation.id,
                f"profile revision conflict: {error}",
            ) from error
        # The Git-backed agent markdown is the durable export other processes
        # sync from. Persist the applied revision there too, otherwise the
        # next file sync would silently revert the approved change.
        if self._knowledge_base is not None:
            self._knowledge_base.save_agent_profile(updated)
        return {"revision": updated.revision, "content_hash": None,
                "agent_id": updated.agent_id}

    def _apply_knowledge(
        self, recommendation: Recommendation, current_content: str | None
    ) -> dict[str, Any]:
        if self._knowledge_base is None:
            raise AnalysisError(
                "knowledge base is not configured for this instance"
            )
        proposed = recommendation.proposed_content
        if not isinstance(proposed, str) or not proposed.strip():
            raise AnalysisError(
                f"{recommendation.target_kind} recommendation "
                "proposed_content must be a non-empty string"
            )
        if recommendation.target_kind == TARGET_KIND_PROJECT:
            path = self._safe_knowledge_write(
                recommendation.target_id,
                proposed,
                self._knowledge_base.create_or_update_project_summary,
            )
        elif recommendation.target_kind == TARGET_KIND_COMPONENT:
            path = self._safe_knowledge_write(
                recommendation.target_id,
                proposed,
                self._knowledge_base.update_component_rules,
            )
        else:
            raise AnalysisError(
                f"unsupported recommendation target_kind: "
                f"{recommendation.target_kind!r}"
            )
        written = path.read_text(encoding="utf-8")
        return {
            "content_hash": content_hash(written),
            "path": str(path),
            "target_kind": recommendation.target_kind,
            "target_id": recommendation.target_id,
        }

    def _safe_knowledge_write(
        self,
        target_id: str,
        proposed: str,
        writer: Callable[[str, str], Path],
    ) -> Path:
        """Run a knowledge writer, mapping a containment failure to a domain error."""
        try:
            return writer(target_id, proposed)
        except KnowledgePathError as error:
            raise AnalysisError(
                f"invalid knowledge target {target_id!r}: {error}"
            ) from error


def _target_exists(
    database: Database,
    knowledge_base: KnowledgeBase | None,
    target_kind: str,
    target_id: str,
) -> bool:
    if target_kind == TARGET_KIND_PROFILE:
        return (
            AgentProfileRepository(database).get_current(target_id) is not None
        )
    path = (
        _knowledge_target_path(knowledge_base, target_kind, target_id)
        if knowledge_base is not None
        else None
    )
    return bool(path is not None and path.is_file())


__all__ = [
    "ANALYZER_AGENT_ID",
    "ANALYZER_READ_ONLY_TOOLS",
    "ANALYZER_OUTPUT_CONTRACT",
    "AnalysisError",
    "AnalysisParseError",
    "AnalysisRequest",
    "AnalyzerRunResult",
    "AnalyzerRunner",
    "RecommendationConflictError",
    "RecommendationPreview",
    "RecommendationService",
    "TARGET_KIND_COMPONENT",
    "TARGET_KIND_PROFILE",
    "TARGET_KIND_PROJECT",
    "TARGET_KINDS",
    "analysis_operation_handler",
    "build_evidence_package",
    "content_hash",
    "default_analyzer_runner",
    "parse_analyzer_output",
    "resolve_analyzer_runner",
]
