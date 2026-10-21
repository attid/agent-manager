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

from kuzya.analysis import (
    ANALYZER_AGENT_ID,
    AnalysisParseError,
    AnalyzerRunResult,
    RecommendationConflictError,
    RecommendationService,
    _render_instructions,
    build_evidence_package,
    content_hash,
    parse_analyzer_output,
)
from kuzya.api import create_app
from kuzya.knowledge import KnowledgeBase
from kuzya.operation_handlers import register_default_operation_handlers
from kuzya.operation_worker import (
    OPERATION_KIND_ANALYSIS,
    OperationInstanceContext,
    OperationWorker,
)
from kuzya.runtime_defaults import RuntimeDefaultsResolver
from kuzya.storage import (
    AgentProfileRepository,
    AnalysisRepository,
    Database,
    OperationRepository,
    RecommendationStateError,
    TaskRepository,
)
from kuzya.telegram.client import BackendClient
from kuzya.telegram.handlers import cmd_analyze

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


def _project_recommendation(target_id: str = "kuzya") -> dict[str, Any]:
    return {
        "target_kind": "project",
        "target_id": target_id,
        "title": "Update project summary",
        "rationale": "The summary omits the analysis workflow.",
        "evidence": ["task:task-one"],
        "proposed_content": "# Проект: Kuzya\n\n- **Порт API**: 18767\n",
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

        # Milestone 2 compaction invariants:
        assert len(evidence["items"]) > 0
        for item in evidence["items"]:
            assert "id" in item
            assert "kind" in item
            assert item.get("content") is None
            assert "content" not in item
            assert set(item.keys()) == {"id", "kind"}

        # Milestone 2 profile enrichment: top-level profile retains system_prompt and runner_config
        assert evidence["profile"] is not None
        assert evidence["profile"]["agent_id"] == "developer"
        assert evidence["profile"]["revision"] == 1
        assert evidence["profile"]["system_prompt"] == "Original prompt."
        assert evidence["profile"]["runner_config"] == {
            "project": "/tmp/project",
            "project_read_only": True,
        }
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
        knowledge.create_or_update_project_summary("kuzya", "Old summary")
        recommendation = _seed_recommendation(
            database,
            tmp_path,
            recommendation=_project_recommendation(),
            knowledge_base=knowledge,
        )
        assert recommendation.target_kind == "project"
        assert recommendation.target_content_hash == content_hash(
            (knowledge.projects_dir / "kuzya" / "summary.md").read_text(
                encoding="utf-8"
            )
        )

        service = RecommendationService(database, knowledge_base=knowledge)
        service.approve(recommendation.id)
        applied = service.apply(recommendation.id)
        assert applied.status == "applied"
        summary = (
            knowledge.projects_dir / "kuzya" / "summary.md"
        ).read_text(encoding="utf-8")
        assert "Порт API" in summary
        assert applied.applied_content_hash == content_hash(summary)

        # A second recommendation generated from the new content applies
        # cleanly; one generated from older content conflicts.
        stale = _seed_recommendation(
            database,
            tmp_path,
            recommendation=_project_recommendation("kuzya"),
            knowledge_base=knowledge,
            task_id="task-two",
        )
        service.approve(stale.id)
        knowledge.create_or_update_project_summary(
            "kuzya", "Even newer summary written by someone else."
        )
        with pytest.raises(RecommendationConflictError):
            service.apply(stale.id)
        current = (
            knowledge.projects_dir / "kuzya" / "summary.md"
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

        for target_id in ("../../escape", "/tmp/kuzya-escape"):
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
        assert not pathlib.Path("/tmp/kuzya-escape").exists()
    finally:
        database.dispose()


# -- Milestone 2: evidence package compaction & Task #40 verification ---


def test_evidence_package_compaction_and_profile_enrichment(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Milestone 2: build_evidence_package must compact items to lightweight references.

    Items must contain only 'id' and 'kind' (no 'content'). Top-level 'profile'
    must contain 'system_prompt' and 'runner_config'.
    """
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        target = tasks.create(
            id="task-compaction",
            title="Compaction verification task",
            agent_id="developer",
            description="Detailed task description",
            metadata={"review": [{"note": "Review item 1", "score": 9}]},
        )
        _make_profile(
            database,
            "developer",
            prompt="Specialized system prompt instructions.",
        )
        operations = OperationRepository(database)
        operations.submit(
            kind="review",
            target_id="task-compaction",
            input={"check": "lint", "status": "clean"},
        )

        evidence = build_evidence_package(database, target)

        # 1. Verify top-level profile contains system_prompt and runner_config
        profile_data = evidence["profile"]
        assert profile_data is not None
        assert profile_data["agent_id"] == "developer"
        assert profile_data["revision"] == 1
        assert profile_data["name"] == "Developer"
        assert profile_data["role"] == "developer"
        assert profile_data["system_prompt"] == "Specialized system prompt instructions."
        assert profile_data["runner_config"] == {
            "project": "/tmp/project",
            "project_read_only": True,
        }

        # 2. Verify all item kinds are present in items catalog
        assert len(evidence["items"]) >= 5
        kinds = {item["kind"] for item in evidence["items"]}
        assert {"task", "profile", "operation", "review", "trace", "metrics"}.issubset(kinds)

        # 3. Invariant: every item in evidence["items"] has only id and kind, NO content
        for item in evidence["items"]:
            assert "id" in item and isinstance(item["id"], str) and item["id"]
            assert "kind" in item and isinstance(item["kind"], str) and item["kind"]
            assert item.get("content") is None
            assert "content" not in item
            assert set(item.keys()) == {"id", "kind"}

        # 4. Invariant: canonical data lives in top-level keys
        assert evidence["task"]["id"] == "task-compaction"
        assert evidence["task"]["description"] == "Detailed task description"
        assert len(evidence["operations"]) == 1
        assert evidence["operations"][0]["kind"] == "review"
        assert len(evidence["reviews"]) == 2  # 1 from metadata, 1 from operation
        assert evidence["trace"]["status"] == "unavailable"  # no artifact roots provided
        assert "operation_count" in evidence["metrics"]

        # 5. Edge case: task without profile has profile=None and no profile item
        unassigned_task = tasks.create(id="task-unassigned", title="No profile")
        unassigned_evidence = build_evidence_package(database, unassigned_task)
        assert unassigned_evidence["profile"] is None
        assert not any(it["kind"] == "profile" for it in unassigned_evidence["items"])
        for item in unassigned_evidence["items"]:
            assert item.get("content") is None
            assert set(item.keys()) == {"id", "kind"}
    finally:
        database.dispose()


def test_evidence_package_payload_reduction_on_large_trace(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Milestone 2 / Task #40: verify significant payload size reduction on large trace."""
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        profiles = AgentProfileRepository(database)
        operations = OperationRepository(database)

        # Substantial system prompt (mimicking production agent contract)
        system_prompt = (
            "You are an autonomous engineering agent operating within a sandbox.\n"
            "Constraints:\n"
            "- Always verify before proposing changes.\n"
            "- Follow the five-component handoff report standard.\n"
        ) * 100  # ~12 KB

        profiles.create(
            agent_id="complex-agent",
            name="Complex Agent",
            role="developer",
            system_prompt=system_prompt,
            runner_config={
                "project": "/tmp/project",
                "network": False,
                "allowed_hosts": ["eurmtl.me", "api.z-ai.io"],
            },
        )

        task = tasks.create(
            id="task-large-trace",
            title="Simulated Task #40 trace workload",
            agent_id="complex-agent",
            description="Large task description with detailed instructions.\n" * 50,
            metadata={
                "review": [
                    {"note": f"Code review round {i}", "approved": True}
                    for i in range(5)
                ]
            },
        )

        # Submit multiple operations
        for i in range(15):
            operations.submit(
                kind="review",
                target_id=task.id,
                input={
                    "step": i,
                    "patch": f"diff --git a/file_{i}.py b/file_{i}.py\n+print('line {i}')\n" * 20,
                },
            )

        # Set up realistic diagnostic trace artifacts
        task_dir = tmp_path / "tasks" / f"task_{task.id}_hash"
        session_file = task_dir / "state" / "sessions" / "session-1" / "pi" / "session.jsonl"
        events_file = task_dir / "state" / "sessions" / "session-1" / "pi" / "events.jsonl"
        session_file.parent.mkdir(parents=True, exist_ok=True)

        session_records = []
        events_records = []
        for i in range(40):
            call_id = f"call_{i:03d}"
            session_records.append({
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_call",
                            "id": call_id,
                            "name": "bash",
                            "arguments": {
                                "command": f"pytest tests/test_module_{i}.py -k 'test_step_{i}'",
                                "cwd": "/workspace",
                            },
                        }
                    ],
                },
            })
            events_records.append({
                "type": "tool_execution_start",
                "toolCallId": call_id,
                "toolName": "bash",
                "args": {"command": f"pytest tests/test_module_{i}.py -k 'test_step_{i}'"},
            })
            events_records.append({
                "type": "tool_execution_end",
                "toolCallId": call_id,
                "isError": False,
                "durationMs": 150 + i,
            })

        session_file.write_text(
            "\n".join(json.dumps(r) for r in session_records) + "\n",
            encoding="utf-8",
        )
        events_file.write_text(
            "\n".join(json.dumps(r) for r in events_records) + "\n",
            encoding="utf-8",
        )

        # Build compacted evidence package
        compacted_evidence = build_evidence_package(
            database, task, artifact_roots=[tmp_path / "tasks"]
        )

        # 1. Structural assertions
        assert compacted_evidence["profile"]["system_prompt"] == system_prompt
        assert compacted_evidence["profile"]["runner_config"]["allowed_hosts"] == [
            "eurmtl.me",
            "api.z-ai.io",
        ]
        assert len(compacted_evidence["items"]) > 20
        for item in compacted_evidence["items"]:
            assert item.get("content") is None
            assert set(item.keys()) == {"id", "kind"}

        # 2. Simulate legacy duplicate structure for exact quantitative comparison
        legacy_items = []
        legacy_items.append({
            "id": f"task:{task.id}",
            "kind": "task",
            "content": compacted_evidence["task"],
        })
        legacy_items.append({
            "id": f"profile:complex-agent@1",
            "kind": "profile",
            "content": compacted_evidence["profile"],
        })
        for op in compacted_evidence["operations"]:
            legacy_items.append({
                "id": f"operation:{op['id']}",
                "kind": "operation",
                "content": op,
            })
        for rev in compacted_evidence["reviews"]:
            legacy_items.append({
                "id": rev["id"],
                "kind": "review",
                "content": rev,
            })
        legacy_items.append({
            "id": "trace:task",
            "kind": "trace",
            "content": compacted_evidence["trace"],
        })
        legacy_items.append({
            "id": "metrics:task",
            "kind": "metrics",
            "content": compacted_evidence["metrics"],
        })

        legacy_evidence = dict(compacted_evidence)
        legacy_evidence["items"] = legacy_items

        compacted_bytes = len(json.dumps(compacted_evidence, ensure_ascii=False))
        legacy_bytes = len(json.dumps(legacy_evidence, ensure_ascii=False))

        # 3. Assert substantial reduction: at least 35% smaller and >15KB savings
        assert compacted_bytes < legacy_bytes * 0.65, (
            f"Expected compacted bytes ({compacted_bytes}) to be < 65% of legacy ({legacy_bytes})"
        )
        assert (legacy_bytes - compacted_bytes) > 15_000, (
            f"Expected savings > 15KB, got {legacy_bytes - compacted_bytes} bytes"
        )
    finally:
        database.dispose()


def test_task40_evidence_package_compaction_and_manifest(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Task #40 evidence package exhibits zero content duplication and proper manifest format."""
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id="developer",
            name="Developer Agent",
            role="coder",
            system_prompt="You are a senior systems engineer.\n" + ("Guidelines: Write robust code.\n" * 50),
            runner_config={"model": "deepseek-coder", "provider": "deepseek", "temperature": 0.2},
        )
        task = tasks.create(
            id="task-40",
            title="Complex multi-stage refactoring and database migration",
            agent_id="developer",
            description="Perform large-scale codebase transformation with 100+ operations and tool invocations.",
            metadata={"priority": "high", "attempt": 3, "tags": ["m2", "migration"]},
        )

        ops = OperationRepository(database)
        for i in range(10):
            ops.submit(
                kind="task_execution" if i % 2 == 0 else "review",
                target_id=task.id,
                input={"step": i, "description": f"Executing sub-stage {i} with extensive parameters"},
                metadata={"iteration": i, "runtime_ms": 1200 + i * 150},
            )

        evidence = build_evidence_package(database, task)

        # Assertion 1: All items in evidence["items"] must NOT contain "content"
        assert "items" in evidence
        assert len(evidence["items"]) >= 12  # task + profile + 10 ops + reviews + trace + metrics
        for item in evidence["items"]:
            assert "id" in item, f"Missing 'id' in item: {item}"
            assert "kind" in item, f"Missing 'kind' in item: {item}"
            assert item.get("content") is None, (
                f"Compaction violation: item {item['id']} ({item['kind']}) still contains 'content'!"
            )
            assert set(item.keys()) == {"id", "kind"}

        # Assertion 2: Top-level profile must contain system_prompt and runner_config
        assert evidence["profile"] is not None
        assert evidence["profile"]["agent_id"] == "developer"
        assert "system_prompt" in evidence["profile"]
        assert "runner_config" in evidence["profile"]
        assert "Guidelines: Write robust code." in evidence["profile"]["system_prompt"]
        assert evidence["profile"]["runner_config"] == {
            "model": "deepseek-coder",
            "provider": "deepseek",
            "temperature": 0.2,
        }

        # Assertion 3: Top-level canonical keys exist and contain full data
        assert evidence["task"]["id"] == "task-40"
        assert len(evidence["operations"]) == 10
        assert "trace" in evidence
        assert "metrics" in evidence
    finally:
        database.dispose()


def test_task40_prompt_rendering_and_serialization(
    migrated_db: pathlib.Path,
) -> None:
    """Task #40 instructions render without error and contain valid parseable JSON."""
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        _make_profile(database, "developer")
        task = tasks.create(id="task-40", title="Refactoring task", agent_id="developer")

        evidence = build_evidence_package(database, task)
        instructions = _render_instructions(task, evidence)

        # 1. Check prompt structure
        assert f"Task: {task.id} — {task.title}" in instructions
        assert "Evidence package (JSON):" in instructions
        assert "Return exactly one JSON object and no prose outside it." in instructions

        # 2. Extract and parse the embedded JSON
        start_marker = "Evidence package (JSON):\n"
        end_marker = "\n\nReturn exactly one JSON object"
        start_idx = instructions.find(start_marker) + len(start_marker)
        end_idx = instructions.find(end_marker)
        embedded_json = instructions[start_idx:end_idx].strip()

        parsed_evidence = json.loads(embedded_json)
        assert parsed_evidence["task_id"] == "task-40"
        assert parsed_evidence["task"]["title"] == "Refactoring task"
    finally:
        database.dispose()


def test_task40_large_prompt_execution_succeeds_without_errno7(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Analysis operation with prompt > 132 KiB executes successfully via fake_pi_runner."""
    database = Database.from_path(migrated_db)
    try:
        tasks = TaskRepository(database)
        task = tasks.create(id="task-40", title="Massive Trace Task")

        # Register system-log-analyzer profile
        profiles = AgentProfileRepository(database)
        profiles.create(
            agent_id=ANALYZER_AGENT_ID,
            name="System Log Analyzer",
            role="analyzer",
            system_prompt="Analyze the session.",
            runner_config={
                "mode": "success",
                "summary_text": _report_text([_profile_recommendation("developer")]),
            },
        )

        # Configure large payload in Task metadata to ensure instructions > 135 KiB (> 128 KiB limit)
        large_padding = "X" * 140_000
        tasks.update(task.id, metadata={"stress_padding": large_padding})

        instance = OperationInstanceContext(
            database=database,
            artifact_root=tmp_path / "operations",
            runner_argv_prefix=(sys.executable, str(FAKE_RUNNER)),
        )
        worker = _worker(database, instance)
        operation = _submit_analysis(database, "task-40")

        # Step operation: must not raise OSError [Errno 7] Argument list too long
        outcome = worker.step()
        assert outcome is not None
        assert outcome.status == "succeeded", (
            f"Operation failed with error: {outcome.error}"
        )

        # Verify persisted report
        reports = AnalysisRepository(database).list_reports(task_id="task-40")
        assert len(reports) == 1
        assert reports[0].agent_id == ANALYZER_AGENT_ID
        assert reports[0].summary == "Observed session facts."
    finally:
        database.dispose()
