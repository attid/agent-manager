"""Isolated deterministic Pirozhki end-to-end acceptance.

Every test in this module composes the real production components against a
disposable instance: a migrated temporary SQLite database, a temporary
Git-backed knowledge base and project repository, the real secretary
dispatcher, developer task worker and durable operation worker, and the real
JavaScript platform tools executed by Node against the ephemeral HTTP backend.
No test inserts an asserted result directly into the database.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from agent_manager.permissions import (
    GIT_ACTIONS,
    REVIEW_TRIGGER,
    actions_for_tools,
    resources_for_execution,
)
from agent_manager.storage import AnalysisRepository

from .pirozhki_harness import (
    PirozhkiInstance,
    pirozhki_instance,
    run_git,
)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_conversation_turn(instance: PirozhkiInstance, text: str) -> dict:
    conversation = instance.post("/conversations", json={"title": "Pirozhki"}).json()
    assert conversation["id"]
    message = instance.post(
        f"/conversations/{conversation['id']}/messages",
        json={"text": text},
    ).json()
    turn = instance.post(
        f"/conversations/{conversation['id']}/turns",
        json={"input_message_id": message["id"]},
    ).json()
    return {
        "conversation_id": conversation["id"],
        "message_id": message["id"],
        "turn_id": turn["id"],
    }


def test_pirozhki_lifecycle_end_to_end(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    with pirozhki_instance(migrated_db, tmp_path / "pirozhki") as instance:
        # 1. A real user turn runs through the secretary dispatcher and the
        #    deterministic child Runner.
        turn = _create_conversation_turn(
            instance,
            "Создай агента pirozhki-developer и поставь задачу на кнопку заказа.",
        )
        outcome = instance.dispatcher.step()
        assert outcome is not None
        assert outcome.status == "completed", outcome.error
        assert outcome.turn.id == turn["turn_id"]
        messages = instance.get(
            f"/conversations/{turn['conversation_id']}/messages"
        ).json()
        assistant = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert "create_task" in assistant[0]["text"]
        # The turn status is completed through the durable protocol.
        stored_turn = instance.get(f"/turns/{turn['turn_id']}").json()
        assert stored_turn["status"] == "completed"

        # 2. The real JavaScript platform tools create the developer agent and
        #    the task, then start it. The response comes from the HTTP backend.
        submitted = instance.run_js_tool("submit")
        assert submitted["ok"], submitted
        task_id = submitted["task_id"]
        assert submitted["agent_id"] == "pirozhki-developer"
        assert submitted["agent_revision"] == 1
        host_path = pathlib.Path(submitted["host_path"])
        assert host_path.resolve() == instance.project_workspace.resolve()

        task_before = instance.get(f"/tasks/{task_id}").json()
        assert task_before["status"] == "in_progress"
        assert task_before["workspace"] == str(instance.project_workspace)

        # 3. The developer worker runs the task in the exact project workspace
        #    and the deterministic child writes the real working-tree change.
        developer_outcome = instance.task_worker.step()
        assert developer_outcome is not None, "developer task was not claimed"
        assert developer_outcome.task_id == task_id
        assert developer_outcome.status == "ready_to_commit", developer_outcome.error
        assert developer_outcome.changed_files == ["index.html"]
        changed = (instance.project_workspace / "index.html").read_text(
            encoding="utf-8"
        )
        assert "Заказать горячий пирожок" in changed
        assert 'class="primary"' in changed
        assert developer_outcome.summary
        # Automatic review ran inside the same developer execution and approved.
        assert "ОДОБРЕНО" in developer_outcome.summary

        task_after_dev = instance.get(f"/tasks/{task_id}").json()
        assert task_after_dev["status"] == "ready_to_commit"
        execution_context = task_after_dev["metadata"]["execution_context"]
        assert execution_context["agent_id"] == "pirozhki-developer"
        assert execution_context["host_path"] == str(instance.project_workspace)
        assert execution_context["sandbox_path"] == "/workspace"

        # 4. A standalone review of the completed execution runs without
        #    rerunning the developer. The JS tool trigger is idempotent for an
        #    exact invocation retry and creates a new operation for a later
        #    intentional invocation.
        review = instance.run_js_tool("review", task_id)
        assert review["ok"], review
        assert review["review_replayed"] == review["review_operation_id"]
        assert review["review_second_operation_id"] != review["review_operation_id"]
        review_outcomes = instance.run_until_idle()
        review_statuses = {o.operation.id: o.status for o in review_outcomes}
        assert review_statuses[review["review_operation_id"]] == "succeeded"
        assert (
            review_statuses[review["review_second_operation_id"]] == "succeeded"
        )
        first_review = instance.operations.get(review["review_operation_id"])
        assert first_review is not None and first_review.result is not None
        assert first_review.result["verdict"] == "approved"
        report_path = pathlib.Path(first_review.result["report_path"])
        assert report_path.is_file()
        assert "ОДОБРЕНО" in report_path.read_text(encoding="utf-8")

        # 5. Evidence-backed analysis produces an evidence-linked report and
        #    recommendations. The analyzer path is the production
        #    ``system-log-analyzer`` profile runner, not an injected callback.
        instance.set_analyzer_evidence(task_id)
        analysis = instance.run_js_tool("analysis", task_id)
        assert analysis["ok"], analysis
        analysis_outcomes = instance.run_until_idle()
        assert analysis_outcomes
        analysis_operation = instance.operations.get(analysis["analysis_operation_id"])
        assert analysis_operation is not None
        assert analysis_operation.status == "succeeded", analysis_operation.error
        assert analysis_operation.result is not None
        assert analysis_operation.result["agent_id"] == "system-log-analyzer"

        report_id = analysis_operation.result["report_id"]
        report = AnalysisRepository(instance.database).get_report(report_id)
        assert report is not None
        assert report.evidence["task_id"] == task_id
        assert report.evidence["task"]["id"] == task_id
        # The evidence is bounded to the target task and its own operations.
        assert "pirozhki" in json.dumps(report.evidence, ensure_ascii=False)

        recommendations = AnalysisRepository(
            instance.database
        ).list_recommendations(report_id=report_id)
        assert len(recommendations) == 2
        profile_recommendation = next(
            r for r in recommendations if r.target_kind == "profile"
        )
        project_recommendation = next(
            r for r in recommendations if r.target_kind == "project"
        )
        assert profile_recommendation.status == "proposed"
        assert profile_recommendation.target_id == "pirozhki-developer"
        assert profile_recommendation.target_revision == 1
        assert profile_recommendation.evidence == [f"task:{task_id}"]

        # 6. The profile recommendation is approved and applied through the
        #    real JavaScript tools, advancing the observed revision.
        applied = instance.run_js_tool("recommend", profile_recommendation.id)
        assert applied["ok"], applied
        assert applied["would_conflict"] is False
        assert applied["approved_status"] == "approved"
        assert applied["applied_status"] == "applied"
        assert applied["applied_revision"] == 2
        developer_profile = instance.agents.get_current("pirozhki-developer")
        assert developer_profile is not None
        assert developer_profile.revision == 2
        assert "зелёная" in developer_profile.system_prompt

        # The applied profile revision survives a knowledge-base file sync,
        # which is what a restart performs.
        instance.knowledge_base.sync_agent_from_file(
            "pirozhki-developer", instance.database
        )
        after_sync = instance.agents.get_current("pirozhki-developer")
        assert after_sync is not None
        assert after_sync.revision >= 2
        assert "зелёная" in after_sync.system_prompt

        # 7. The project recommendation updates the Git-backed knowledge base.
        project_apply = instance.post(
            f"/recommendations/{project_recommendation.id}/approve"
        )
        assert project_apply.status_code == 200, project_apply.text
        project_apply = instance.post(
            f"/recommendations/{project_recommendation.id}/apply"
        )
        assert project_apply.status_code == 200, project_apply.text
        summary_path = (
            instance.knowledge_base.projects_dir / "pirozhki-shop" / "summary.md"
        )
        assert summary_path.is_file()
        summary_text = summary_path.read_text(encoding="utf-8")
        assert "index.html" in summary_text
        knowledge_log = run_git(instance.knowledge_base.root_dir, "log", "--oneline")
        assert "Update summary for project: pirozhki-shop" in knowledge_log.stdout

        # 8. A later analysis captures the new revision; an external writer
        #    then advances it, so applying the stale proposal conflicts and
        #    never overwrites the newer content.
        stale_operation = instance.post(
            "/operations",
            json={
                "kind": "analysis",
                "target_id": task_id,
                "idempotency_key": "second-analysis",
            },
        )
        assert stale_operation.status_code == 202, stale_operation.text
        stale_outcomes = instance.run_until_idle()
        assert stale_outcomes
        stale_report_id = stale_outcomes[-1].result["report_id"]
        stale_recommendations = AnalysisRepository(
            instance.database
        ).list_recommendations(report_id=stale_report_id)
        stale_profile = next(
            r for r in stale_recommendations if r.target_kind == "profile"
        )
        assert stale_profile.target_revision == 2
        instance.agents.update(
            agent_id="pirozhki-developer",
            expected_revision=2,
            system_prompt="Внешняя правка промпта.",
        )
        approved = instance.post(f"/recommendations/{stale_profile.id}/approve")
        assert approved.status_code == 200, approved.text
        conflicted = instance.post(f"/recommendations/{stale_profile.id}/apply")
        assert conflicted.status_code == 409, conflicted.text
        stored = AnalysisRepository(instance.database).get_recommendation(
            stale_profile.id
        )
        assert stored is not None and stored.status == "conflict"
        newest = instance.agents.get_current("pirozhki-developer")
        assert newest is not None and newest.revision == 3
        assert newest.system_prompt == "Внешняя правка промпта."

        # 9. Local commit of the task's real change, then the task is done.
        commit_request = instance.post(
            "/operations",
            json={
                "kind": "git.commit_push",
                "target_id": task_id,
                "input": {"push": False, "message": "feat(pirozhki): кнопка заказа"},
                "idempotency_key": "pirozhki-commit",
            },
        )
        assert commit_request.status_code == 202, commit_request.text
        commit_outcomes = instance.run_until_idle()
        commit_result = next(
            o for o in commit_outcomes if o.operation.kind == "git.commit_push"
        )
        assert commit_result.status == "succeeded", commit_result.error
        assert commit_result.result is not None
        assert commit_result.result["commit_hash"]
        assert commit_result.result["task_status"] == "done"
        log = run_git(instance.project_workspace, "log", "-1", "--format=%s")
        assert "кнопка заказа" in log.stdout
        assert instance.get(f"/tasks/{task_id}").json()["status"] == "done"

        # 10. Archiving removes the profile file and a later sync does not
        #     resurrect it.
        archived = instance.delete("/agents/pirozhki-developer")
        assert archived.status_code == 200, archived.text
        assert archived.json()["runner_config"]["archived"] is True
        agent_file = (
            instance.knowledge_base.agents_dir / "pirozhki-developer.md"
        )
        assert not agent_file.exists()
        archive_log = run_git(
            instance.knowledge_base.root_dir, "log", "--oneline"
        )
        assert "Archive agent profile: pirozhki-developer" in archive_log.stdout

        # A restart-style bootstrap re-reads files and exports DB profiles.
        instance.knowledge_base.initialize(database=instance.database)
        still_archived = instance.agents.get_current("pirozhki-developer")
        assert still_archived is not None
        assert still_archived.runner_config["archived"] is True
        active_ids = {
            profile.agent_id
            for profile in instance.agents.list_current(include_archived=False)
        }
        assert "pirozhki-developer" not in active_ids
        assert not agent_file.exists()


def test_restart_preserves_state_and_replays_operations_idempotently(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    root = tmp_path / "restart"
    with pirozhki_instance(migrated_db, root) as first:
        submitted = first.run_js_tool("submit")
        assert submitted["ok"], submitted
        task_id = submitted["task_id"]
        first.task_worker.step()

        conversation = first.post(
            "/conversations", json={"title": "Durable"}
        ).json()
        message = first.post(
            f"/conversations/{conversation['id']}/messages",
            json={"text": "Сохрани это."},
        ).json()
        turn = first.post(
            f"/conversations/{conversation['id']}/turns",
            json={"input_message_id": message["id"]},
        ).json()
        assert first.dispatcher.step().status == "completed"

        review = first.run_js_tool("review", task_id)
        assert review["ok"], review
        operation_id = review["review_operation_id"]
        replay = first.post(
            "/operations",
            json={
                "kind": "review",
                "target_id": task_id,
                "idempotency_key": "restart-review-key",
            },
        )
        assert replay.status_code == 202, replay.text
        # An exact keyed repeat returns the same operation without writing.
        replay_same = first.post(
            "/operations",
            json={
                "kind": "review",
                "target_id": task_id,
                "idempotency_key": "restart-review-key",
            },
        )
        assert replay_same.status_code == 200, replay_same.text
        assert replay_same.json()["id"] == replay.json()["id"]

    # Rebuild the whole instance against the same database and paths.
    with pirozhki_instance(migrated_db, root) as second:
        fetched_conversation = second.get(f"/conversations/{conversation['id']}")
        assert fetched_conversation.status_code == 200, fetched_conversation.text
        messages = second.get(
            f"/conversations/{conversation['id']}/messages"
        ).json()
        assert any(m["id"] == message["id"] for m in messages)
        stored_turn = second.get(f"/turns/{turn['id']}")
        assert stored_turn.status_code == 200
        assert stored_turn.json()["status"] == "completed"

        task = second.get(f"/tasks/{task_id}").json()
        assert task["status"] == "ready_to_commit"

        # The queued operations survived the restart and a new worker can
        # still process them.
        outcomes = second.run_until_idle()
        assert any(o.operation.id == operation_id for o in outcomes)
        persisted = second.operations.get(operation_id)
        assert persisted is not None
        assert persisted.status == "succeeded"

        # The same idempotency key still resolves to the same stored operation
        # after restart instead of creating a duplicate.
        replay_after_restart = second.post(
            "/operations",
            json={
                "kind": "review",
                "target_id": task_id,
                "idempotency_key": "restart-review-key",
            },
        )
        assert replay_after_restart.status_code == 200, replay_after_restart.text
        assert replay_after_restart.json()["id"] == replay.json()["id"]
        assert (
            len(
                [
                    op
                    for op in second.operations.list(kind="review", target_id=task_id)
                    if op.id == operation_id
                ]
            )
            == 1
        )

        # A startup worker reconciles an orphaned running operation instead of
        # silently replaying it.
        orphan = second.operations.submit(kind="git.rollback", target_id=task_id).operation
        claimed = second.operations.claim_next(worker_id="crashed-worker")
        assert claimed is not None and claimed.id == orphan.id
        interrupted = second.operation_worker.reconcile_startup_orphans()
        assert orphan.id in interrupted
        recovered = second.operations.get(orphan.id)
        assert recovered is not None and recovered.status == "interrupted"


def test_review_without_verdict_fails_strictly(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A review that produces no explicit verdict never approves by absence."""
    with pirozhki_instance(migrated_db, tmp_path / "strict") as instance:
        submitted = instance.run_js_tool("submit")
        assert submitted["ok"], submitted
        task_id = submitted["task_id"]
        assert instance.task_worker.step().status == "ready_to_commit"

        reviewer = instance.agents.get_current("system-code-reviewer")
        assert reviewer is not None
        config = dict(reviewer.runner_config or {})
        config["summary_text"] = (
            "Изменения выглядят нормально, критичных замечаний нет."
        )
        instance.agents.update(
            agent_id="system-code-reviewer",
            expected_revision=reviewer.revision,
            runner_config=config,
        )

        submitted_review = instance.post(
            "/operations",
            json={
                "kind": "review",
                "target_id": task_id,
                "idempotency_key": "strict-review",
            },
        )
        assert submitted_review.status_code == 202, submitted_review.text
        outcomes = instance.run_until_idle()
        review_outcome = next(
            o for o in outcomes if o.operation.kind == "review"
        )
        assert review_outcome.status == "failed", review_outcome.result
        stored = instance.operations.get(submitted_review.json()["id"])
        assert stored is not None
        assert "invalid_report" in (stored.error or "")
        # The task is not approved by the absence of a verdict.
        task = instance.get(f"/tasks/{task_id}").json()
        assert task["metadata"]["review"]["verdict"] is None


def test_negative_scoped_api_permissions_on_populated_state(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    with pirozhki_instance(migrated_db, tmp_path / "permissions") as instance:
        first = instance.post(
            "/tasks", json={"title": "Own task", "status": "incoming"}
        ).json()
        second = instance.post(
            "/tasks", json={"title": "Foreign task", "status": "incoming"}
        ).json()
        assert first["id"] != second["id"]

        # A developer execution may read its own task but not enumerate the
        # populated queue or read a foreign task.
        read_actions = actions_for_tools(["list_tasks", "get_task"])
        read_resources = resources_for_execution(
            read_actions, task_id=first["id"], agent_id="pirozhki-developer"
        )
        read_grant = instance.permissions.issue(
            agent_id="pirozhki-developer",
            actions=read_actions,
            resources=read_resources,
            label=f"task:{first['id']}",
        )
        own = instance.get(
            f"/tasks/{first['id']}", headers=_bearer(read_grant.token)
        )
        assert own.status_code == 200, own.text
        foreign = instance.get(
            f"/tasks/{second['id']}", headers=_bearer(read_grant.token)
        )
        assert foreign.status_code == 403, foreign.text
        collection = instance.get("/tasks", headers=_bearer(read_grant.token))
        assert collection.status_code == 403, collection.text
        write = instance.post(
            "/tasks",
            json={"title": "Not allowed"},
            headers=_bearer(read_grant.token),
        )
        assert write.status_code == 403, write.text

        # A review-scoped execution cannot submit a Git operation even though
        # the operation route itself accepts several kinds.
        review_actions = actions_for_tools(["trigger_review"])
        assert review_actions == frozenset({REVIEW_TRIGGER})
        review_grant = instance.permissions.issue(
            agent_id="system-code-reviewer",
            actions=review_actions,
            resources=resources_for_execution(
                review_actions, task_id=first["id"], agent_id="system-code-reviewer"
            ),
            label=f"task:{first['id']}",
        )
        review_submit = instance.post(
            "/operations",
            json={"kind": "review", "target_id": first["id"]},
            headers=_bearer(review_grant.token),
        )
        assert review_submit.status_code == 202, review_submit.text
        git_submit = instance.post(
            "/operations",
            json={"kind": "git.commit_push", "target_id": first["id"]},
            headers=_bearer(review_grant.token),
        )
        assert git_submit.status_code == 403, git_submit.text
        assert actions_for_tools(["trigger_commit_push"]) == frozenset({GIT_ACTIONS})

        # Without a credential the API rejects the request instead of falling
        # back to an implicit administrative identity.
        anonymous = httpx.get(f"{instance.base_url}/tasks", timeout=10.0)
        assert anonymous.status_code == 401, anonymous.text
