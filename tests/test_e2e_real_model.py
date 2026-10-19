"""Opt-in real-model acceptance for the isolated Pirozhki instance.

These tests are skipped unless ``AGENT_MANAGER_REAL_MODEL_E2E`` is truthy.
They then compose the production ``pi_runner`` and the real default system
profiles against a disposable instance and drive the platform through the
normal conversation/turn and operation APIs with whatever provider/model the
host Pi runtime resolves through the normal mechanisms.

No credential is read, echoed or persisted by these tests, and no production
Telegram, API or data path is touched. When the environment cannot provide a
working model/runtime the test fails with the actual runner diagnostic rather
than reporting a synthetic success.

The lifecycle test exercises real agent execution: the secretary actually
invokes platform tools to create work, the developer actually edits the
temporary project, the reviewer and analyzer actually run through their
production profiles, and explicit user approval/apply/commit/archive go
through the normal APIs. Simulated explicit user approvals are permitted;
the tests never impersonate an agent execution or override its output.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

import pytest

from agent_manager.runtime_defaults import RuntimeDefaultsResolver
from agent_manager.storage import AnalysisRepository

from .pirozhki_harness import REPO_ROOT, PirozhkiInstance, pirozhki_instance, run_git

_TRUTHY = {"1", "true", "yes", "on"}


def _real_model_enabled() -> bool:
    return os.environ.get("AGENT_MANAGER_REAL_MODEL_E2E", "").strip().lower() in _TRUTHY


pytestmark = pytest.mark.skipif(
    not _real_model_enabled(),
    reason=(
        "opt-in real-model acceptance: set AGENT_MANAGER_REAL_MODEL_E2E=1 "
        "to run against the host Pi runtime"
    ),
)


def _ensure_src_on_path() -> None:
    src = str(REPO_ROOT / "src")
    existing = os.environ.get("PYTHONPATH", "")
    if src not in existing.split(os.pathsep):
        os.environ["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")


def _require_bubblewrap() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is required for a real Pi execution")


def _assistant_text(instance: PirozhkiInstance, conversation_id: str) -> str:
    messages = instance.get(f"/conversations/{conversation_id}/messages").json()
    return "\n".join(
        m.get("text", "") for m in messages if m.get("role") == "assistant"
    )


def _conversation_turn(instance: PirozhkiInstance, text: str) -> dict:
    conversation = instance.post("/conversations", json={"title": "Pirozhki"}).json()
    assert conversation["id"]
    message = instance.post(
        f"/conversations/{conversation['id']}/messages", json={"text": text}
    ).json()
    turn = instance.post(
        f"/conversations/{conversation['id']}/turns",
        json={"input_message_id": message["id"]},
    ).json()
    outcome = instance.dispatcher.step()
    return {
        "conversation_id": conversation["id"],
        "message_id": message["id"],
        "turn_id": turn["id"],
        "outcome": outcome,
    }


def _task_failure(instance: PirozhkiInstance, task_id: str) -> str:
    task = instance.get(f"/tasks/{task_id}").json()
    metadata = task.get("metadata") or {}
    return (
        f"task status={task.get('status')!r} "
        f"error={metadata.get('error')!r} summary={metadata.get('summary')!r}"
    )


def _seed_developer_profile(instance: PirozhkiInstance, workspace: pathlib.Path) -> None:
    """Seed the executor profile as test setup.

    This is fixture configuration, not an agent execution: the profile only
    describes the executor. The developer still performs the real edit through
    the production task worker, and the secretary still assigns the work
    through its real platform tools.
    """
    response = instance.post(
        "/agents",
        json={
            "agent_id": "pirozhki-developer",
            "name": "Pirozhki Developer",
            "role": "developer",
            "system_prompt": (
                "# Pirozhki Developer\n\n"
                "You develop the pirozhki-shop storefront in the configured "
                "workspace. Implement the requested change in the real project "
                "files using write/edit, then report a concise factual summary "
                "of the change you made.\n"
            ),
            "runner_config": {
                "project": str(workspace),
                "network": True,
                "project_read_only": False,
                "tools": ["read", "write", "edit", "bash", "read_knowledge"],
            },
        },
    )
    assert response.status_code in (200, 201), response.text


def test_real_model_secretary_conversation_turn(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    _require_bubblewrap()
    _ensure_src_on_path()

    with pirozhki_instance(
        migrated_db,
        tmp_path / "real-model",
        runner_argv_prefix=(sys.executable, "-m", "pi_runner"),
        seed_deterministic_configs=False,
        runtime_defaults=RuntimeDefaultsResolver(),
    ) as instance:
        conversation = instance.post(
            "/conversations", json={"title": "Real model"}
        ).json()
        message = instance.post(
            f"/conversations/{conversation['id']}/messages",
            json={"text": "Ответь одним коротким предложением: система готова."},
        ).json()
        turn = instance.post(
            f"/conversations/{conversation['id']}/turns",
            json={"input_message_id": message["id"]},
        ).json()
        outcome = instance.dispatcher.step()
        assert outcome is not None, "no turn was claimed"
        assert outcome.status == "completed", (
            "real-model turn failed; runner diagnostic follows:\n"
            f"{outcome.error}"
        )
        assert outcome.reply is not None
        assert outcome.reply.text.strip(), "real model returned an empty reply"
        stored_turn = instance.get(f"/turns/{turn['id']}").json()
        assert stored_turn["status"] == "completed"


def test_real_model_pirozhki_lifecycle(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Drive the Pirozhki lifecycle with real agent executions.

    The secretary creates and routes the work through its real tools, the
    developer and reviewer run through the production task worker, the
    analyzer runs through the durable analysis operation, and the explicit
    user decisions (start, approve, apply, commit, archive) go through the
    normal APIs.
    """
    _require_bubblewrap()
    _ensure_src_on_path()

    with pirozhki_instance(
        migrated_db,
        tmp_path / "real-lifecycle",
        runner_argv_prefix=(sys.executable, "-m", "pi_runner"),
        seed_deterministic_configs=False,
        runtime_defaults=RuntimeDefaultsResolver(),
    ) as instance:
        workspace = instance.project_workspace.resolve()
        _seed_developer_profile(instance, workspace)

        # 1. Real secretary turn: the model must use platform tools to create
        #    and route the work, not just describe it.
        prompt = (
            "Создай и поставь в работу задачу для агента pirozhki-developer.\n"
            f"Рабочий каталог проекта (host path): {workspace}\n"
            "Название задачи: «Поменять дизайн кнопки заказа».\n"
            "Описание задачи: в проекте pirozhki-shop открой файл index.html и сделай "
            "кнопку заказа зелёной (CSS-класс primary), текст кнопки — "
            "«Заказать горячий пирожок».\n"
            "Создай задачу сразу со статусом in_progress. Не задавай уточняющих "
            "вопросов, все данные уже даны."
        )
        turn = _conversation_turn(instance, prompt)
        outcome = turn["outcome"]
        assert outcome is not None, "no secretary turn was claimed"
        assert outcome.status == "completed", (
            "real-model secretary turn failed; runner diagnostic follows:\n"
            f"{outcome.error}"
        )
        assistant = _assistant_text(instance, turn["conversation_id"])
        tasks = instance.get("/tasks").json()
        assigned = [
            task
            for task in tasks
            if task.get("agent_id") == "pirozhki-developer"
            and str(task.get("workspace") or "") == str(workspace)
        ]
        assert assigned, (
            "secretary did not create the assigned task through its tools; "
            f"assistant reply:\n{assistant}"
        )
        task = assigned[-1]
        task_id = task["id"]
        assert "кнопк" in task["title"].lower() or "order" in task["title"].lower()

        # An incoming draft needs an explicit user decision to start. This is
        # a simulated user action, not an agent execution.
        if task["status"] != "in_progress":
            started = instance.patch(
                f"/tasks/{task_id}", json={"status": "in_progress"}
            )
            assert started.status_code == 200, started.text

        # This acceptance runs the reviewer explicitly as a single bounded
        # review operation below. Keep the developer execution out of the
        # automatic multi-round review loop so the run stays bounded; the
        # user always controls review policy through task metadata.
        metadata = dict(task.get("metadata") or {})
        metadata["requires_review"] = False
        metadata["task_type"] = "development"
        configured = instance.patch(
            f"/tasks/{task_id}", json={"metadata": metadata}
        )
        assert configured.status_code == 200, configured.text

        # 2. Real developer execution in the exact project workspace. The
        #    production task worker runs the developer profile against the
        #    real project so the model must actually edit the storefront.
        developer = instance.task_worker.step()
        assert developer is not None, "developer task was not claimed"
        assert developer.task_id == task_id
        assert developer.status in ("ready_to_commit", "completed"), (
            "real developer execution failed: "
            f"{developer.error}; {_task_failure(instance, task_id)}"
        )
        assert "index.html" in developer.changed_files, (
            f"developer did not change index.html: {developer.changed_files}"
        )
        status = run_git(workspace, "status", "--porcelain")
        assert "index.html" in status.stdout, status.stdout
        assert developer.summary, "developer produced no summary"
        # When the task was configured as a reviewed coding task the automatic
        # review is part of the same execution and must carry a real verdict.
        if developer.status == "ready_to_commit":
            assert "ОДОБРЕНО" in developer.summary or "ТРЕБУЮТСЯ" in developer.summary, (
                f"automatic review produced no verdict: {developer.summary!r}"
            )

        task_after_dev = instance.get(f"/tasks/{task_id}").json()
        assert task_after_dev["status"] in (
            "ready_to_commit",
            "waiting_for_input",
            "done",
        )

        # 3. A standalone review operation runs the real reviewer profile
        #    against the completed execution (without rerunning the developer).
        review = instance.post(
            "/operations",
            json={
                "kind": "review",
                "target_id": task_id,
                "input": {"instructions": "Проверить изменение кнопки заказа"},
                "idempotency_key": "real-review",
            },
        )
        assert review.status_code == 202, review.text
        review_outcomes = instance.run_until_idle(limit=8)
        review_outcome = next(
            (o for o in review_outcomes if o.operation.kind == "review"), None
        )
        assert review_outcome is not None, "review operation was not processed"
        assert review_outcome.status == "succeeded", (
            f"real review failed: {review_outcome.error}"
        )
        review_report = review_outcome.result
        assert review_report and review_report.get("verdict") in (
            "approved",
            "changes_requested",
        ), review_report

        # 4. Real analysis operation: the production analyzer profile produces
        #    an evidence-backed report with recommendations.
        analysis = instance.post(
            "/operations",
            json={
                "kind": "analysis",
                "target_id": task_id,
                "idempotency_key": "real-analysis",
            },
        )
        assert analysis.status_code == 202, analysis.text
        analysis_outcomes = instance.run_until_idle(limit=8)
        analysis_outcome = next(
            (o for o in analysis_outcomes if o.operation.kind == "analysis"), None
        )
        assert analysis_outcome is not None, "analysis operation was not processed"
        assert analysis_outcome.status == "succeeded", (
            f"real analysis failed: {analysis_outcome.error}"
        )
        assert analysis_outcome.result is not None
        report_id = analysis_outcome.result["report_id"]
        report = AnalysisRepository(instance.database).get_report(report_id)
        assert report is not None
        assert report.evidence["task_id"] == task_id
        recommendations = AnalysisRepository(
            instance.database
        ).list_recommendations(report_id=report_id)
        assert recommendations, "real analyzer produced no recommendations"

        # 5. Explicit user approval then apply of one evidence-linked
        #    recommendation through the normal API.
        chosen = recommendations[0]
        approved = instance.post(f"/recommendations/{chosen.id}/approve")
        assert approved.status_code == 200, approved.text
        applied = instance.post(f"/recommendations/{chosen.id}/apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"

        # 6. Local Git acceptance: commit through the durable operation.
        commit = instance.post(
            "/operations",
            json={
                "kind": "git.commit_push",
                "target_id": task_id,
                "input": {
                    "push": False,
                    "message": "feat(pirozhki): кнопка заказа",
                },
                "idempotency_key": "real-commit",
            },
        )
        assert commit.status_code == 202, commit.text
        commit_outcomes = instance.run_until_idle(limit=8)
        commit_outcome = next(
            (o for o in commit_outcomes if o.operation.kind == "git.commit_push"),
            None,
        )
        assert commit_outcome is not None, "commit operation was not processed"
        assert commit_outcome.status == "succeeded", (
            f"real commit failed: {commit_outcome.error}"
        )
        assert commit_outcome.result and commit_outcome.result.get("commit_hash")
        log = run_git(workspace, "log", "-1", "--format=%s")
        assert "кнопка заказа" in log.stdout, log.stdout

        # 7. Archival and restart must not resurrect the archived profile.
        archived = instance.delete("/agents/pirozhki-developer")
        assert archived.status_code == 200, archived.text
        agent_file = (
            instance.knowledge_base.agents_dir / "pirozhki-developer.md"
        )
        assert not agent_file.exists()
        instance.knowledge_base.initialize(database=instance.database)
        still_archived = instance.agents.get_current("pirozhki-developer")
        assert still_archived is not None
        assert still_archived.runner_config.get("archived") is True
        assert not agent_file.exists()
