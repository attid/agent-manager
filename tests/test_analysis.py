"""Evidence-backed analysis and approval-gated recommendation acceptance tests.

Every test runs against a real migrated temporary file database, the real
``OperationWorker`` and a deterministic fixture analyzer runner. No model,
network or external service is involved.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_manager.analysis import (
    ANALYZER_AGENT_ID,
    AnalysisParseError,
    AnalyzerRunResult,
    RecommendationConflictError,
    RecommendationService,
    build_evidence_package,
    content_hash,
    parse_analyzer_output,
)
from agent_manager.api import create_app
from agent_manager.knowledge import KnowledgeBase
from agent_manager.operation_handlers import register_default_operation_handlers
from agent_manager.operation_worker import (
    OPERATION_KIND_ANALYSIS,
    OperationInstanceContext,
    OperationWorker,
)
from agent_manager.runtime_defaults import RuntimeDefaultsResolver
from agent_manager.storage import (
    AgentProfileRepository,
    AnalysisRepository,
    Database,
    OperationRepository,
    RecommendationStateError,
    TaskRepository,
)
from agent_manager.telegram.client import BackendClient
from agent_manager.telegram.handlers import cmd_analyze

FAKE_RUNNER = pathlib.Path(__file__).with_name("fake_pi_runner.py")


# -- fixtures and helpers ------------------------------------------------


def _report_text(
    recommendations: list[dict[str, Any]],
    *,
    summary: str = "Observed session facts.",
    findings: list[dict[str, Any]] | None = None,
) -> str:
    document = {
        "summary": summary,
        "findings": findings if findings is not None else [],
        "recommendations": recommendations,
    }
    return "```json\n" + json.dumps(document, ensure_ascii=False) + "\n```"


def _runner(text: str, *, revision: int | None = 3):
    """Return a fixture analyzer runner producing a fixed text report."""

    def run(request: Any) -> AnalyzerRunResult:
        return AnalyzerRunResult(
            text=text,
            agent_id=ANALYZER_AGENT_ID,
            profile_revision=revision,
        )

    return run


def _boom_runner(request: Any) -> AnalyzerRunResult:
    """Fixture analyzer runner that always fails."""
    raise RuntimeError("boom")


def _profile_recommendation(
    target_id: str = "developer",
    *,
    proposed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "target_kind": "profile",
        "target_id": target_id,
        "title": "Tighten the prompt",
        "rationale": "The session repeatedly searched for missing rules.",
        "evidence": ["task:task-one"],
        "proposed_content": (
            proposed
            if proposed is not None
            else {"system_prompt": "Updated developer prompt."}
        ),
    }


def _project_recommendation(target_id: str = "agent-manager") -> dict[str, Any]:
    return {
        "target_kind": "project",
        "target_id": target_id,
        "title": "Update project summary",
        "rationale": "The summary omits the analysis workflow.",
        "evidence": ["task:task-one"],
        "proposed_content": "# Проект: Agent Manager\n\n- **Порт API**: 18767\n",
    }


def _make_profile(
    database: Database,
    agent_id: str = "developer",
    *,
    prompt: str = "Original prompt.",
) -> None:
    AgentProfileRepository(database).create(
        agent_id=agent_id,
        name="Developer",
        role="developer",
        system_prompt=prompt,
        runner_config={"project": "/tmp/project", "project_read_only": True},
    )


def _instance(
    database: Database,
    tmp_path: pathlib.Path,
    *,
    runner,
    knowledge_base: KnowledgeBase | None = None,
) -> OperationInstanceContext:
    return OperationInstanceContext(
        database=database,
        artifact_root=tmp_path / "operations",
        knowledge_base=knowledge_base,
        analyzer_runner=runner,
    )


def _worker(
    database: Database, instance: OperationInstanceContext
) -> OperationWorker:
    worker = OperationWorker(database, instance=instance)
    register_default_operation_handlers(worker)
    return worker


def _submit_analysis(
    database: Database, task_id: str, *, key: str | None = None
):
    return OperationRepository(database).submit(
        kind=OPERATION_KIND_ANALYSIS,
        target_id=task_id,
        input={},
        idempotency_key=key,
    ).operation


# -- parser --------------------------------------------------------------


def test_parse_analyzer_output_accepts_raw_and_fenced_objects() -> None:
    payload = {"summary": "ok", "recommendations": []}
    assert parse_analyzer_output(json.dumps(payload)) == payload
    assert parse_analyzer_output("```json\n" + json.dumps(payload) + "\n```") == payload


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no json here",
        "{not valid json}",
        '{"recommendations": []}',
        '{"summary": "x", "recommendations": "no"}',
        '{"summary": "x", "recommendations": [{"target_kind": "bogus"}]}',
    ],
)
def test_parse_analyzer_output_rejects_malformed_documents(text: str) -> None:
    with pytest.raises(AnalysisParseError):
        parse_analyzer_output(text)


# -- evidence package ----------------------------------------------------


def test_evidence_package_is_bounded_to_the_target_task(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        target = tasks.create(
            id="task-one",
            title="Target task",
            agent_id="developer",
            metadata={"summary": "target-only-summary"},
        )
        tasks.create(
            id="task-two",
            title="Unrelated task",
            metadata={"summary": "unrelated-only-summary"},
        )
        _make_profile(database, "developer")
        _make_profile(database, "other-agent")
        operations = OperationRepository(database)
        operations.submit(kind="review", target_id="task-one", input={"n": 1})
        unrelated_operation = operations.submit(
            kind="review", target_id="task-two", input={"n": 2}
        ).operation

        evidence = build_evidence_package(database, target)
        assert evidence["task_id"] == "task-one"
        serialized = json.dumps(evidence, ensure_ascii=False)
        assert "target-only-summary" in serialized
        assert "unrelated-only-summary" not in serialized
        assert unrelated_operation.id not in serialized
        # Only the target's profile, not the unrelated agent's profile.
        item_ids = {item["id"] for item in evidence["items"]}
        assert "profile:developer@1" in item_ids
        assert "profile:other-agent@1" not in item_ids
    finally:
        database.dispose()


# -- executor dispatch ---------------------------------------------------


def test_analysis_operation_dispatch_persists_report_and_recommendations(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        TaskRepository(database).create(id="task-one", title="Implement feature")
        _make_profile(database, "developer", prompt="Original prompt.")
        runner = _runner(_report_text([_profile_recommendation()]))
        worker = _worker(database, _instance(database, tmp_path, runner=runner))
        assert OPERATION_KIND_ANALYSIS in worker.registered_kinds()

        operation = _submit_analysis(database, "task-one", key="analysis-key")
        outcome = worker.step()
        assert outcome is not None and outcome.status == "succeeded"
        stored = OperationRepository(database).get(operation.id)
        assert stored is not None and stored.result is not None
        assert stored.result["report_id"]
        assert stored.result["agent_id"] == ANALYZER_AGENT_ID
        assert stored.result["summary"] == "Observed session facts."

        reports = AnalysisRepository(database).list_reports(task_id="task-one")
        assert len(reports) == 1
        recommendations = AnalysisRepository(database).list_recommendations(
            report_id=reports[0].id
        )
        assert len(recommendations) == 1
        recommendation = recommendations[0]
        assert recommendation.status == "proposed"
        assert recommendation.target_kind == "profile"
        assert recommendation.target_revision == 1
        assert recommendation.evidence == ["task:task-one"]
        # Generation captured the observed profile revision but did not mutate.
        current = AgentProfileRepository(database).get_current("developer")
        assert current is not None
        assert current.revision == 1
        assert current.system_prompt == "Original prompt."
    finally:
        database.dispose()


def test_missing_task_fails_visibly(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        worker = _worker(
            database,
            _instance(database, tmp_path, runner=_runner(_report_text([]))),
        )
        operation = _submit_analysis(database, "does-not-exist")
        outcome = worker.step()
        assert outcome is not None and outcome.status == "failed"
        stored = OperationRepository(database).get(operation.id)
        assert stored is not None
        assert "task not found: does-not-exist" in (stored.error or "")
        assert stored.result is None
        assert AnalysisRepository(database).list_reports(task_id="does-not-exist") == []
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "runner_factory, expected",
    [
        (lambda: _boom_runner, "analyzer run failed: RuntimeError: boom"),
        (lambda: _runner(""), "analyzer returned an empty report"),
        (lambda: _runner("no structured report"), "malformed analyzer report"),
    ],
)
def test_analyzer_failures_are_visible(
    migrated_db: pathlib.Path,
    tmp_path: pathlib.Path,
    runner_factory,
    expected: str,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        TaskRepository(database).create(id="task-one", title="Task")
        worker = _worker(
            database, _instance(database, tmp_path, runner=runner_factory())
        )
        operation = _submit_analysis(database, "task-one")
        outcome = worker.step()
        assert outcome is not None and outcome.status == "failed"
        stored = OperationRepository(database).get(operation.id)
        assert stored is not None
        assert expected in (stored.error or "")
        assert stored.result is None
        assert AnalysisRepository(database).list_reports(task_id="task-one") == []
    finally:
        database.dispose()


# -- recommendation workflow --------------------------------------------


def _seed_recommendation(
    database: Database,
    tmp_path: pathlib.Path,
    *,
    recommendation: dict[str, Any],
    knowledge_base: KnowledgeBase | None = None,
    task_id: str = "task-one",
):
    TaskRepository(database).create(id=task_id, title="Task")
    runner = _runner(_report_text([recommendation]))
    worker = _worker(
        database,
        _instance(database, tmp_path, runner=runner, knowledge_base=knowledge_base),
    )
    operation = _submit_analysis(database, task_id)
    outcome = worker.step()
    assert outcome is not None and outcome.status == "succeeded"
    report = AnalysisRepository(database).list_reports(task_id=task_id)[0]
    return AnalysisRepository(database).list_recommendations(report_id=report.id)[0]


def test_approval_is_explicit_and_generation_does_not_mutate_profile(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _make_profile(database, "developer", prompt="Original prompt.")
        recommendation = _seed_recommendation(
            database, tmp_path, recommendation=_profile_recommendation()
        )
        assert recommendation.status == "proposed"

        current = AgentProfileRepository(database).get_current("developer")
        assert current is not None and current.revision == 1

        service = RecommendationService(database)
        preview = service.preview(recommendation.id)
        assert preview.target_exists is True
        assert preview.would_conflict is False
        assert preview.current_revision == 1

        approved = service.approve(recommendation.id)
        assert approved.status == "approved"
        # Approval alone still changes nothing.
        unchanged = AgentProfileRepository(database).get_current("developer")
        assert unchanged is not None and unchanged.revision == 1

        applied = service.apply(recommendation.id)
        assert applied.status == "applied"
        assert applied.applied_revision == 2
        updated = AgentProfileRepository(database).get_current("developer")
        assert updated is not None
        assert updated.revision == 2
        assert updated.system_prompt == "Updated developer prompt."
    finally:
        database.dispose()


def test_profile_recommendation_accepts_full_prompt_string(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The analyzer contract allows the full proposed text for a profile.

    Applying that document must replace the profile system prompt instead of
    rejecting it with "proposed_content must be an object".
    """
    database = Database.from_path(migrated_db)
    try:
        _make_profile(database, "developer", prompt="Original prompt.")
        recommendation = _seed_recommendation(
            database,
            tmp_path,
            recommendation=_profile_recommendation(
                proposed="# Developer\n\nFollow the acceptance criteria."
            ),
        )
        service = RecommendationService(database)
        service.approve(recommendation.id)
        applied = service.apply(recommendation.id)
        assert applied.status == "applied"
        assert applied.applied_revision == 2
        updated = AgentProfileRepository(database).get_current("developer")
        assert updated is not None
        assert updated.revision == 2
        assert (
            updated.system_prompt
            == "# Developer\n\nFollow the acceptance criteria."
        )
    finally:
        database.dispose()


def test_profile_recommendation_ignores_server_managed_identity_fields(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Echoed identity/revision fields must not fail an otherwise valid apply.

    The analyzer often copies ``agent_id`` and ``revision`` from the observed
    profile into the proposal. The target is fixed by ``target_id`` and the
    revision is re-checked separately, so those fields are ignored instead of
    treated as unsupported.
    """
    database = Database.from_path(migrated_db)
    try:
        _make_profile(database, "developer", prompt="Original prompt.")
        recommendation = _seed_recommendation(
            database,
            tmp_path,
            recommendation=_profile_recommendation(
                proposed={
                    "agent_id": "developer",
                    "revision": 1,
                    "system_prompt": "Updated prompt.",
                }
            ),
        )
        service = RecommendationService(database)
        service.approve(recommendation.id)
        applied = service.apply(recommendation.id)
        assert applied.status == "applied"
        assert applied.applied_revision == 2
        updated = AgentProfileRepository(database).get_current("developer")
        assert updated is not None
        assert updated.revision == 2
        assert updated.system_prompt == "Updated prompt."
    finally:
        database.dispose()


def test_applied_profile_recommendation_survives_knowledge_file_sync(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """An applied profile proposal must not be reverted by a file sync.

    The Git-backed ``agents/<id>.md`` export is what a restart/bootstrap
    imports. Applying only to the database would let the next
    ``sync_agent_from_file`` resurrect the pre-approval prompt.
    """
    database = Database.from_path(migrated_db)
    try:
        _make_profile(database, "developer", prompt="Original prompt.")
        knowledge = KnowledgeBase(tmp_path / "knowledge")
        profiles = AgentProfileRepository(database)
        current = profiles.get_current("developer")
        assert current is not None
        knowledge.save_agent_profile(current)

        recommendation = _seed_recommendation(
            database,
            tmp_path,
            recommendation=_profile_recommendation(),
            knowledge_base=knowledge,
        )
        service = RecommendationService(database, knowledge_base=knowledge)
        service.approve(recommendation.id)
        applied = service.apply(recommendation.id)
        assert applied.status == "applied"

        exported = (
            knowledge.agents_dir / "developer.md"
        ).read_text(encoding="utf-8")
        assert "Updated developer prompt." in exported

        # A restart-style import reads the exported file and must agree with
        # the applied revision instead of reverting it.
        knowledge.sync_agent_from_file("developer", database)
        after = profiles.get_current("developer")
        assert after is not None
        assert after.revision == 2
        assert after.system_prompt == "Updated developer prompt."
    finally:
        database.dispose()


def test_stale_approval_conflicts_without_overwriting_newer_content(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _make_profile(database, "developer", prompt="Original prompt.")
        recommendation = _seed_recommendation(
            database, tmp_path, recommendation=_profile_recommendation()
        )
        service = RecommendationService(database)
        service.approve(recommendation.id)

        # A different writer advances the profile after the proposal.
        profiles = AgentProfileRepository(database)
        profiles.update(
            agent_id="developer",
            expected_revision=1,
            system_prompt="Newer external prompt.",
        )

        with pytest.raises(RecommendationConflictError):
            service.apply(recommendation.id)

        stored = AnalysisRepository(database).get_recommendation(recommendation.id)
        assert stored is not None and stored.status == "conflict"
        newest = profiles.get_current("developer")
        assert newest is not None
        assert newest.revision == 2
        assert newest.system_prompt == "Newer external prompt."
    finally:
        database.dispose()


def test_apply_requires_approval(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _make_profile(database, "developer")
        recommendation = _seed_recommendation(
            database, tmp_path, recommendation=_profile_recommendation()
        )
        service = RecommendationService(database)
        with pytest.raises(RecommendationStateError):
            service.apply(recommendation.id)
        # Still proposed and the target is untouched.
        stored = AnalysisRepository(database).get_recommendation(recommendation.id)
        assert stored is not None and stored.status == "proposed"
        current = AgentProfileRepository(database).get_current("developer")
        assert current is not None and current.revision == 1
    finally:
        database.dispose()


def test_project_recommendation_applies_and_detects_stale_content(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        knowledge = KnowledgeBase(tmp_path / "knowledge")
        knowledge.create_or_update_project_summary("agent-manager", "Old summary")
        recommendation = _seed_recommendation(
            database,
            tmp_path,
            recommendation=_project_recommendation(),
            knowledge_base=knowledge,
        )
        assert recommendation.target_kind == "project"
        assert recommendation.target_content_hash == content_hash(
            (knowledge.projects_dir / "agent-manager" / "summary.md").read_text(
                encoding="utf-8"
            )
        )

        service = RecommendationService(database, knowledge_base=knowledge)
        service.approve(recommendation.id)
        applied = service.apply(recommendation.id)
        assert applied.status == "applied"
        summary = (
            knowledge.projects_dir / "agent-manager" / "summary.md"
        ).read_text(encoding="utf-8")
        assert "Порт API" in summary
        assert applied.applied_content_hash == content_hash(summary)

        # A second recommendation generated from the new content applies
        # cleanly; one generated from older content conflicts.
        stale = _seed_recommendation(
            database,
            tmp_path,
            recommendation=_project_recommendation("agent-manager"),
            knowledge_base=knowledge,
            task_id="task-two",
        )
        service.approve(stale.id)
        knowledge.create_or_update_project_summary(
            "agent-manager", "Even newer summary written by someone else."
        )
        with pytest.raises(RecommendationConflictError):
            service.apply(stale.id)
        current = (
            knowledge.projects_dir / "agent-manager" / "summary.md"
        ).read_text(encoding="utf-8")
        assert "Even newer summary" in current
        stored = AnalysisRepository(database).get_recommendation(stale.id)
        assert stored is not None and stored.status == "conflict"
    finally:
        database.dispose()


def test_default_analyzer_runner_executes_system_log_analyzer(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    try:
        TaskRepository(database).create(id="task-one", title="Task")
        AgentProfileRepository(database).create(
            agent_id=ANALYZER_AGENT_ID,
            name="System Log Analyzer",
            role="analyzer",
            system_prompt="Analyze the session and propose changes.",
            runner_config={
                "mode": "success",
                "summary_text": _report_text(
                    [_profile_recommendation("developer")]
                ),
            },
        )
        _make_profile(database, "developer")
        # No injected runner: the executor must build the production path that
        # freezes and launches ``system-log-analyzer`` through the Pi adapter.
        instance = OperationInstanceContext(
            database=database,
            artifact_root=tmp_path / "operations",
            runner_argv_prefix=(sys.executable, str(FAKE_RUNNER)),
        )
        worker = _worker(database, instance)
        operation = _submit_analysis(database, "task-one")
        outcome = worker.step()
        assert outcome is not None and outcome.status == "succeeded", (
            outcome.error if outcome else "no outcome"
        )

        report = AnalysisRepository(database).list_reports(task_id="task-one")[0]
        assert report.agent_id == ANALYZER_AGENT_ID
        assert report.profile_revision == 1
        recommendations = AnalysisRepository(database).list_recommendations(
            report_id=report.id
        )
        assert recommendations
        assert recommendations[0].target_kind == "profile"
        assert recommendations[0].target_revision == 1
    finally:
        database.dispose()


def test_default_analyzer_runner_uses_instance_runtime_defaults(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The production analyzer must follow the instance runtime selection.

    Without the instance resolver the frozen analyzer role has no
    provider/model and a real host launch cannot authenticate.
    """
    database = Database.from_path(migrated_db)
    try:
        TaskRepository(database).create(id="task-one", title="Task")
        AgentProfileRepository(database).create(
            agent_id=ANALYZER_AGENT_ID,
            name="System Log Analyzer",
            role="analyzer",
            system_prompt="Analyze the session and propose changes.",
            runner_config={
                "mode": "success",
                "summary_text": _report_text(
                    [_profile_recommendation("developer")]
                ),
            },
        )
        _make_profile(database, "developer")
        app_config = tmp_path / "defaults.toml"
        app_config.write_text(
            '[default]\nprovider = "instance-provider"\n'
            'model = "instance-model"\n',
            encoding="utf-8",
        )
        instance = OperationInstanceContext(
            database=database,
            artifact_root=tmp_path / "operations",
            runner_argv_prefix=(sys.executable, str(FAKE_RUNNER)),
            runtime_defaults=RuntimeDefaultsResolver(
                app_config_path=app_config
            ),
        )
        worker = _worker(database, instance)
        _submit_analysis(database, "task-one")
        outcome = worker.step()
        assert outcome is not None and outcome.status == "succeeded", (
            outcome.error if outcome else "no outcome"
        )
        roles = list(
            (tmp_path / "operations" / "analysis").glob("*/runner_role.json")
        )
        assert roles, "analyzer role export missing"
        role = json.loads(roles[-1].read_text(encoding="utf-8"))
        assert role["provider"] == "instance-provider"
        assert role["model"] == "instance-model"
        assert role["pi_auth_provider"] == "instance-provider"
    finally:
        database.dispose()


# -- HTTP API ------------------------------------------------------------


def test_api_exposes_reports_and_recommendation_workflow(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    knowledge = KnowledgeBase(tmp_path / "knowledge")
    try:
        _make_profile(database, "developer")
        recommendation = _seed_recommendation(
            database, tmp_path, recommendation=_profile_recommendation()
        )
        report = AnalysisRepository(database).get_report(
            recommendation.report_id
        )
        assert report is not None

        with TestClient(
            create_app(f"sqlite:///{migrated_db}", knowledge_base=knowledge)
        ) as client:
            listed = client.get("/tasks/task-one/analysis")
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()] == [report.id]

            detail = client.get(f"/analysis/{report.id}")
            assert detail.status_code == 200
            assert detail.json()["summary"] == "Observed session facts."

            recs = client.get(f"/analysis/{report.id}/recommendations")
            assert recs.status_code == 200
            assert [item["id"] for item in recs.json()] == [recommendation.id]

            preview = client.get(f"/recommendations/{recommendation.id}")
            assert preview.status_code == 200
            body = preview.json()
            assert body["recommendation"]["status"] == "proposed"
            assert body["would_conflict"] is False

            # Applying before approval is a state conflict.
            not_approved = client.post(
                f"/recommendations/{recommendation.id}/apply"
            )
            assert not_approved.status_code == 409

            approved = client.post(
                f"/recommendations/{recommendation.id}/approve"
            )
            assert approved.status_code == 200
            assert approved.json()["status"] == "approved"

            applied = client.post(
                f"/recommendations/{recommendation.id}/apply"
            )
            assert applied.status_code == 200
            assert applied.json()["status"] == "applied"

            filtered = client.get(
                "/recommendations",
                params={"task_id": "task-one", "status": "applied"},
            )
            assert filtered.status_code == 200
            assert [item["id"] for item in filtered.json()] == [
                recommendation.id
            ]

            missing = client.get("/analysis/does-not-exist")
            assert missing.status_code == 404
    finally:
        database.dispose()


# -- secretary client and /analyze --------------------------------------


def _backend_client(app) -> BackendClient:
    transport = httpx.ASGITransport(app=app)
    http_client = httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    )
    return BackendClient(base_url="http://testserver", client=http_client)


def test_secretary_client_reads_analysis_results(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    database = Database.from_path(migrated_db)
    knowledge = KnowledgeBase(tmp_path / "knowledge")
    try:
        _make_profile(database, "developer")
        recommendation = _seed_recommendation(
            database, tmp_path, recommendation=_profile_recommendation()
        )
        report = AnalysisRepository(database).get_report(
            recommendation.report_id
        )
        assert report is not None

        app = create_app(f"sqlite:///{migrated_db}", knowledge_base=knowledge)

        async def scenario() -> None:
            client = _backend_client(app)
            try:
                reports = await client.list_analysis_reports("task-one")
                assert reports and reports[0]["id"] == report.id
                detail = await client.get_analysis_report(report.id)
                assert detail["summary"] == "Observed session facts."
                recs = await client.list_report_recommendations(report.id)
                assert recs[0]["id"] == recommendation.id
                preview = await client.preview_recommendation(recommendation.id)
                assert preview["would_conflict"] is False
                approved = await client.approve_recommendation(recommendation.id)
                assert approved["status"] == "approved"
                applied = await client.apply_recommendation(recommendation.id)
                assert applied["status"] == "applied"
            finally:
                await client.close()

        with TestClient(app):
            asyncio.run(scenario())
    finally:
        database.dispose()


def test_cmd_analyze_renders_actual_report_not_hardcoded_findings() -> None:
    message = MagicMock()
    message.text = "/analyze task-one"
    message.answer = AsyncMock()

    class StubClient:
        async def get_task(self, task_id: str) -> dict[str, Any]:
            return {"id": task_id, "title": "Real task", "status": "done"}

        async def list_analysis_reports(
            self, task_id: str
        ) -> list[dict[str, Any]]:
            return []

        async def trigger_analysis(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
            return {"id": "op-1", "status": "pending"}

        async def get_operation(self, operation_id: str) -> dict[str, Any]:
            return {
                "id": operation_id,
                "status": "succeeded",
                "result": {"report_id": "report-1"},
                "error": None,
            }

        async def get_analysis_report(self, report_id: str) -> dict[str, Any]:
            return {
                "id": report_id,
                "agent_id": ANALYZER_AGENT_ID,
                "profile_revision": 3,
                "summary": "The build needed two extra searches.",
            }

        async def list_report_recommendations(
            self, report_id: str
        ) -> list[dict[str, Any]]:
            return [
                {
                    "id": "rec-1",
                    "status": "proposed",
                    "target_kind": "component",
                    "target_id": "build",
                    "title": "Document the build step",
                }
            ]

    asyncio.run(cmd_analyze(message, client=StubClient()))

    message.answer.assert_awaited_once()
    rendered = message.answer.call_args[0][0]
    assert "The build needed two extra searches." in rendered
    assert "Document the build step" in rendered
    assert "component:build" in rendered
    assert "Сессия выполнена успешно" not in rendered


def test_cmd_analyze_shows_pending_and_failure_truthfully() -> None:
    async def run(operation: dict[str, Any]) -> str:
        message = MagicMock()
        message.text = "/analyze task-one"
        message.answer = AsyncMock()

        class StubClient:
            async def get_task(self, task_id: str) -> dict[str, Any]:
                return {"id": task_id, "title": "Real task", "status": "done"}

            async def list_analysis_reports(
                self, task_id: str
            ) -> list[dict[str, Any]]:
                return []

            async def trigger_analysis(self, task_id: str, **kwargs: Any):
                return {"id": "op-1", "status": "pending"}

            async def get_operation(self, operation_id: str):
                return operation

        await cmd_analyze(message, client=StubClient())
        return message.answer.call_args[0][0]

    pending = asyncio.run(
        run(
            {
                "id": "op-1",
                "status": "running",
                "result": None,
                "error": None,
            }
        )
    )
    assert "запущен" in pending
    assert "op-1" in pending

    failed = asyncio.run(
        run(
            {
                "id": "op-1",
                "status": "failed",
                "result": None,
                "error": "RuntimeError: analyzer exploded",
            }
        )
    )
    assert "не удался" in failed
    assert "analyzer exploded" in failed


def test_cmd_analyze_displays_stored_report_without_launching_analysis() -> None:
    message = MagicMock()
    message.text = "/analyze task-one"
    message.answer = AsyncMock()

    class StubClient:
        def __init__(self) -> None:
            self.trigger_calls: list[dict[str, Any]] = []

        async def get_task(self, task_id: str) -> dict[str, Any]:
            return {"id": task_id, "title": "Real task", "status": "done"}

        async def list_analysis_reports(
            self, task_id: str
        ) -> list[dict[str, Any]]:
            return [{"id": "report-1"}]

        async def trigger_analysis(self, task_id: str, **kwargs: Any):
            self.trigger_calls.append(kwargs)
            return {"id": "op-new", "status": "pending"}

        async def get_analysis_report(self, report_id: str) -> dict[str, Any]:
            return {
                "id": report_id,
                "agent_id": ANALYZER_AGENT_ID,
                "profile_revision": 2,
                "summary": "Stored analysis summary.",
            }

        async def list_report_recommendations(
            self, report_id: str
        ) -> list[dict[str, Any]]:
            return []

    client = StubClient()
    asyncio.run(cmd_analyze(message, client=client))

    # A stored result is rendered directly; no model call is submitted.
    assert client.trigger_calls == []
    rendered = message.answer.call_args[0][0]
    assert "Stored analysis summary." in rendered
    assert "refresh" in rendered


def test_cmd_analyze_refresh_forces_a_new_analysis() -> None:
    message = MagicMock()
    message.text = "/analyze task-one refresh"
    message.answer = AsyncMock()

    class StubClient:
        def __init__(self) -> None:
            self.keys: list[str | None] = []

        async def get_task(self, task_id: str) -> dict[str, Any]:
            return {
                "id": task_id,
                "title": "Real task",
                "status": "done",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }

        async def list_analysis_reports(
            self, task_id: str
        ) -> list[dict[str, Any]]:
            return [{"id": "report-old"}]

        async def trigger_analysis(self, task_id: str, **kwargs: Any):
            self.keys.append(kwargs.get("idempotency_key"))
            return {"id": "op-new", "status": "pending"}

        async def get_operation(self, operation_id: str) -> dict[str, Any]:
            return {
                "id": operation_id,
                "status": "running",
                "result": None,
                "error": None,
            }

    client = StubClient()
    asyncio.run(cmd_analyze(message, client=client))

    # The explicit refresh bypasses the stored report and does not reuse a
    # task-scoped idempotency key, so a fresh analysis can start.
    assert client.keys == [None]


def test_cmd_analyze_replays_pending_and_reads_existing_report(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Real handler/client: retries replay one operation, reads never launch."""
    database = Database.from_path(migrated_db)
    knowledge = KnowledgeBase(tmp_path / "knowledge")
    try:
        task = TaskRepository(database).create(
            title="Analyze me", description="v1", status="done"
        )
        app = create_app(f"sqlite:///{migrated_db}", knowledge_base=knowledge)

        async def submit_twice_and_change_evidence() -> None:
            client = _backend_client(app)
            try:
                for _ in range(2):
                    message = MagicMock()
                    message.text = f"/analyze {task.id}"
                    message.answer = AsyncMock()
                    await cmd_analyze(message, client=client)
                # Editing the task changes its revision, so the next request
                # is an intentional fresh analysis, not a stale replay.
                await client.patch_task(task.id, description="v2")
                third = MagicMock()
                third.text = f"/analyze {task.id}"
                third.answer = AsyncMock()
                await cmd_analyze(third, client=client)
            finally:
                await client.close()

        with TestClient(app):
            asyncio.run(submit_twice_and_change_evidence())

        operations = OperationRepository(database).list(
            kind=OPERATION_KIND_ANALYSIS, target_id=task.id
        )
        assert len(operations) == 2, [op.id for op in operations]

        # A stored report is rendered without creating another operation.
        AnalysisRepository(database).create_report(
            task_id=task.id,
            agent_id=ANALYZER_AGENT_ID,
            profile_revision=1,
            summary="Stored analysis summary.",
            report={
                "summary": "Stored analysis summary.",
                "recommendations": [],
            },
            evidence={"task_id": task.id},
        )

        async def render_existing() -> str:
            client = _backend_client(app)
            try:
                message = MagicMock()
                message.text = f"/analyze {task.id}"
                message.answer = AsyncMock()
                await cmd_analyze(message, client=client)
                return message.answer.call_args[0][0]
            finally:
                await client.close()

        with TestClient(app):
            rendered = asyncio.run(render_existing())

        assert "Stored analysis summary." in rendered
        assert (
            len(
                OperationRepository(database).list(
                    kind=OPERATION_KIND_ANALYSIS, target_id=task.id
                )
            )
            == 2
        )
    finally:
        database.dispose()



def test_analysis_rejects_recommendation_target_outside_knowledge_base(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A traversal/absolute target is rejected, never written or persisted."""
    database = Database.from_path(migrated_db)
    try:
        TaskRepository(database).create(id="task-one", title="Task")
        knowledge = KnowledgeBase(tmp_path / "knowledge")
        knowledge.projects_dir.mkdir(parents=True, exist_ok=True)

        for target_id in ("../../escape", "/tmp/agent-manager-escape"):
            runner = _runner(
                _report_text([_project_recommendation(target_id)])
            )
            worker = _worker(
                database,
                _instance(
                    database,
                    tmp_path,
                    runner=runner,
                    knowledge_base=knowledge,
                ),
            )
            operation = _submit_analysis(database, "task-one")
            outcome = worker.step()
            assert outcome is not None
            assert outcome.status == "failed", target_id
            assert "outside the knowledge base" in (outcome.error or "")
            # The malicious proposal is never persisted as a recommendation.
            assert (
                AnalysisRepository(database).list_recommendations(
                    task_id="task-one"
                )
                == []
            )

        assert not (tmp_path / "escape").exists()
        assert not pathlib.Path("/tmp/agent-manager-escape").exists()
    finally:
        database.dispose()
