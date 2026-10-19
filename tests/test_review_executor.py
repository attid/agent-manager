"""Unit tests for the shared review executor and report validation.

Every test runs in-process against real temporary files. The Runner child is
replaced by an injected callable, so no model, network or real Pi process is
involved. The validation contract under test is that a review is approved only
when the reviewer produced an explicit, well-formed verdict.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from agent_manager.review_executor import (
    DEFAULT_REVIEWER_PROMPT,
    ReviewExecutionError,
    ReviewExecutor,
    ReviewFormatError,
    ReviewRequest,
    VERDICT_APPROVED,
    VERDICT_CHANGES_REQUESTED,
    parse_review_report,
)


def _request(tmp_path: Path, **overrides: object) -> ReviewRequest:
    values: dict[str, object] = {
        "task_id": "task-1",
        "task_title": "Fix pagination",
        "task_description": "Fix the pagination bug.",
        "project_dir": tmp_path,
        "changed_files": ("src/app.py",),
        "developer_summary": "Fixed it.",
        "reviewer_system_prompt": "You are a reviewer.",
        "reviewer_runner_config": {"provider": "test-provider", "model": "test-model"},
        "round_number": 1,
        "state_dir": tmp_path / "review" / "state",
        "artifact_dir": tmp_path / "review",
        "task_artifact_dir": tmp_path / "artifacts" / "task-1",
    }
    values.update(overrides)
    return ReviewRequest(**values)  # type: ignore[arg-type]


def _executor(
    runner: object,
    *,
    knowledge_base: object | None = None,
) -> ReviewExecutor:
    return ReviewExecutor(
        runner_argv_prefix=["fake-runner"],
        runtime_defaults=_passive_resolver(),
        knowledge_base=knowledge_base,
        subprocess_run=runner,
    )


def _passive_resolver():
    from agent_manager.runtime_defaults import RuntimeDefaultsResolver

    return RuntimeDefaultsResolver(global_settings_path="/nonexistent/settings.json")


def _fake_runner(
    summary_path: Path,
    text: str,
    *,
    status: str = "completed",
    returncode: int = 0,
    stdout: str | None = None,
    stderr: str = "",
):
    def run(cmd, **kwargs):  # noqa: ANN001 - mirrors subprocess.run
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(text, encoding="utf-8")
        payload = stdout if stdout is not None else json.dumps(
            {"status": status, "summary_path": str(summary_path)}
        )
        return subprocess.CompletedProcess(cmd, returncode, payload, stderr)

    return run


# -- report validation ---------------------------------------------------


def test_parse_explicit_approved_text_verdict() -> None:
    verdict, findings = parse_review_report(
        "Код проверен.\nВЕРДИКТ: ОДОБРЕНО\n"
    )
    assert verdict == VERDICT_APPROVED
    assert findings == ()


def test_parse_changes_requested_with_evidence_linked_finding() -> None:
    report = (
        "P1: src/auth/token.py:42 — токен не проверяется на срок действия.\n"
        "ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ\n"
    )
    verdict, findings = parse_review_report(report)
    assert verdict == VERDICT_CHANGES_REQUESTED
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "P1"
    assert finding.file == "src/auth/token.py"
    assert finding.line == 42
    assert "токен" in finding.message
    assert finding.evidence is not None


def test_parse_structured_json_block_is_authoritative() -> None:
    report = (
        "Review body.\n"
        "```review\n"
        '{"verdict": "changes_requested", "findings": ['
        '{"severity": "P1", "file": "src/app.py", "line": 7, '
        '"message": "off by one", "evidence": "for i in range(n-1)"}]}\n'
        "```\n"
    )
    verdict, findings = parse_review_report(report)
    assert verdict == VERDICT_CHANGES_REQUESTED
    assert findings[0].file == "src/app.py"
    assert findings[0].line == 7


def test_parse_absent_verdict_fails_instead_of_approving() -> None:
    with pytest.raises(ReviewFormatError):
        parse_review_report("Looks good to me, no P1 problems found.")


def test_parse_ambiguous_verdict_markers_fail() -> None:
    report = "ВЕРДИКТ: ОДОБРЕНО\nВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ\n"
    with pytest.raises(ReviewFormatError):
        parse_review_report(report)


def test_parse_malformed_json_block_fails() -> None:
    report = "```review\n{not valid json}\n```\n"
    with pytest.raises(ReviewFormatError):
        parse_review_report(report)


def test_parse_approved_with_critical_finding_fails() -> None:
    report = (
        "P1: src/app.py:1 — boom\n"
        "ВЕРДИКТ: ОДОБРЕНО\n"
    )
    with pytest.raises(ReviewFormatError):
        parse_review_report(report)


def test_parse_changes_requested_without_findings_fails() -> None:
    with pytest.raises(ReviewFormatError):
        parse_review_report("ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ\n")


def test_parse_empty_report_fails() -> None:
    with pytest.raises(ReviewFormatError):
        parse_review_report("   ")


# -- executor success path ----------------------------------------------


def test_execute_valid_approved_round_persists_report_and_metrics(
    tmp_path: Path,
) -> None:
    summary = tmp_path / "out" / "summary.md"
    request = _request(tmp_path)
    executor = _executor(
        _fake_runner(summary, "Проверено.\nВЕРДИКТ: ОДОБРЕНО\n")
    )

    result = executor.execute(request)

    assert result.verdict == VERDICT_APPROVED
    assert result.approved is True
    assert result.findings == ()
    assert Path(result.report_path).is_file()
    assert Path(result.metrics_path).is_file()
    metrics = json.loads(Path(result.metrics_path).read_text(encoding="utf-8"))
    assert metrics["verdict"] == "APPROVED"
    assert metrics["domain_verdict"] == "approved"
    assert metrics["approved"] is True
    assert metrics["runtime"]["provider"] == "test-provider"
    assert metrics["runtime"]["model"] == "test-model"

    # The generated role is strictly read-only and carries the resolved model.
    role = json.loads((tmp_path / "review" / "role_r1.json").read_text(encoding="utf-8"))
    assert role["project_read_only"] is True
    assert role["read_only"] is True
    assert role["tools"] == ["read", "bash"]
    assert "allow_git_commit" not in role
    assert role["provider"] == "test-provider"
    assert role["model"] == "test-model"
    assert role["agent_id"] == "system-code-reviewer"
    # The reviewer's own instructions file reached the role.
    assert any(
        str(tmp_path / "review" / "reviewer_system_prompt_r1.md") == entry
        for entry in role["instructions"]
    )


def test_execute_changes_requested_round_keeps_findings(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    report = (
        "P1: src/app.py:9 — missing null check\n"
        "ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ\n"
    )
    executor = _executor(_fake_runner(summary, report))

    result = executor.execute(_request(tmp_path))

    assert result.verdict == VERDICT_CHANGES_REQUESTED
    assert result.approved is False
    assert result.findings[0].file == "src/app.py"
    assert result.findings[0].line == 9
    assert result.to_dict()["verdict"] == "changes_requested"


def test_execute_uses_default_prompt_when_profile_missing(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    executor = _executor(
        _fake_runner(summary, "ВЕРДИКТ: ОДОБРЕНО\n")
    )
    request = _request(
        tmp_path, reviewer_system_prompt="", reviewer_runner_config={}
    )
    executor.execute(request)
    prompt = (
        tmp_path / "review" / "reviewer_system_prompt_r1.md"
    ).read_text(encoding="utf-8")
    assert prompt == DEFAULT_REVIEWER_PROMPT


def test_execute_adds_knowledge_base_to_readonly_paths(tmp_path: Path) -> None:
    class _Knowledge:
        root_dir = tmp_path / "kb"

    _Knowledge.root_dir.mkdir()
    summary = tmp_path / "summary.md"
    executor = _executor(
        _fake_runner(summary, "ВЕРДИКТ: ОДОБРЕНО\n"), knowledge_base=_Knowledge()
    )
    executor.execute(_request(tmp_path))
    role = json.loads((tmp_path / "review" / "role_r1.json").read_text(encoding="utf-8"))
    assert str(_Knowledge.root_dir) in role["readonly_paths"]


# -- executor failure path ----------------------------------------------


def test_execute_nonzero_exit_fails_with_diagnostics(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    executor = _executor(
        _fake_runner(
            summary,
            "ВЕРДИКТ: ОДОБРЕНО\n",
            returncode=3,
            stderr="reviewer crashed",
        )
    )

    with pytest.raises(ReviewExecutionError) as excinfo:
        executor.execute(_request(tmp_path))

    error = excinfo.value
    assert error.diagnostics["reason"] == "runner_nonzero_exit"
    assert "reviewer crashed" in error.diagnostics["stderr"]
    assert error.diagnostics_path is not None
    assert Path(error.diagnostics_path).is_file()
    # A nonzero exit is never an approval even with an approving body.
    assert not (tmp_path / "review" / "round_1.md").exists()


def test_execute_failed_runner_status_fails(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    executor = _executor(
        _fake_runner(summary, "ВЕРДИКТ: ОДОБРЕНО\n", status="failed")
    )
    with pytest.raises(ReviewExecutionError) as excinfo:
        executor.execute(_request(tmp_path))
    assert excinfo.value.diagnostics["reason"] == "runner_status_not_completed"


def test_execute_malformed_stdout_fails(tmp_path: Path) -> None:
    executor = _executor(
        _fake_runner(tmp_path / "summary.md", "", stdout="not json at all")
    )
    with pytest.raises(ReviewExecutionError) as excinfo:
        executor.execute(_request(tmp_path))
    assert excinfo.value.diagnostics["reason"] == "malformed_runner_output"


def test_execute_empty_report_fails(tmp_path: Path) -> None:
    executor = _executor(_fake_runner(tmp_path / "summary.md", "   "))
    with pytest.raises(ReviewExecutionError) as excinfo:
        executor.execute(_request(tmp_path))
    assert excinfo.value.diagnostics["reason"] == "empty_report"


def test_execute_invalid_verdict_fails_with_persisted_diagnostics(
    tmp_path: Path,
) -> None:
    executor = _executor(
        _fake_runner(tmp_path / "summary.md", "No problems found.\n")
    )
    with pytest.raises(ReviewExecutionError) as excinfo:
        executor.execute(_request(tmp_path))
    assert excinfo.value.diagnostics["reason"] == "invalid_report"
    diagnostics = json.loads(
        Path(excinfo.value.diagnostics_path).read_text(encoding="utf-8")
    )
    assert diagnostics["reason"] == "invalid_report"


def test_execute_missing_report_file_fails(tmp_path: Path) -> None:
    executor = _executor(
        _fake_runner(
            tmp_path / "summary.md",
            "ВЕРДИКТ: ОДОБРЕНО\n",
            stdout=json.dumps(
                {"status": "completed", "summary_path": str(tmp_path / "absent.md")}
            ),
        )
    )
    with pytest.raises(ReviewExecutionError) as excinfo:
        executor.execute(_request(tmp_path))
    assert excinfo.value.diagnostics["reason"] == "missing_report"


def test_execute_requires_artifact_dir(tmp_path: Path) -> None:
    executor = _executor(_fake_runner(tmp_path / "s.md", "x"))
    request = _request(tmp_path, artifact_dir=None)
    with pytest.raises(ValueError):
        executor.execute(request)
