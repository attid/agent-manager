"""Machine-readable capability inventory, executable parity and discovery.

The parity test is deliberately executable: it enumerates the real tools
registered by ``src/pi_runner/policy.mjs`` through Node and compares them with
the live FastAPI OpenAPI document. When Node or the Pi TypeBox runtime is
absent the tool-enumeration test skips, but the OpenAPI-side parity and every
inventory invariant still run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_manager.api import create_app
from agent_manager.capabilities import (
    CAPABILITIES,
    ENDPOINT_CAPABILITIES,
    HANDBOOK,
    TOOL_BY_NAME,
    TOOL_CAPABILITIES,
    audit_capabilities,
    executable_tool_names,
    inventory_document,
    is_platform_agent,
    render_handbook_discovery,
    write_handbook_discovery,
)
from agent_manager.knowledge import KnowledgeBase
from agent_manager.permissions import actions_for_tools
from agent_manager.storage import AgentProfileRepository, Database

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_SOURCE = REPO_ROOT / "src/pi_runner/policy.mjs"


def _typebox_runtime() -> Path | None:
    releases = Path.home() / ".pi/agent/install/releases"
    return next(
        iter(sorted(releases.glob("*/node_modules/typebox"), reverse=True)),
        None,
    )


def _prepare_policy(tmp_path: Path) -> Path | None:
    node = shutil.which("node")
    typebox = _typebox_runtime()
    if node is None or typebox is None:
        return None
    policy = tmp_path / "policy.mjs"
    shutil.copyfile(POLICY_SOURCE, policy)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/typebox").symlink_to(
        typebox, target_is_directory=True
    )
    return policy


def _run_node(script: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    assert node is not None
    return subprocess.run(
        [node, str(script), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(cwd) if cwd is not None else None,
    )


def _registered_tool_names(tmp_path: Path) -> list[str] | None:
    policy = _prepare_policy(tmp_path)
    if policy is None:
        return None
    result = _run_node(
        Path(__file__).with_name("policy_tool_names.mjs"),
        str(policy),
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _openapi_paths(migrated_db: Path) -> dict:
    app = create_app(f"sqlite:///{migrated_db}")
    return app.openapi()["paths"]


def test_inventory_parity_with_openapi_and_registered_tools(
    tmp_path: Path, migrated_db: Path
) -> None:
    paths = _openapi_paths(migrated_db)
    tool_names = _registered_tool_names(tmp_path)
    audit = audit_capabilities(
        openapi_paths=paths,
        registered_tool_names=tool_names,
    )
    assert audit.ok, "\n".join(
        f"{finding.code}: {finding.message}" for finding in audit.findings
    )


def test_inventory_declares_every_tool_once_and_documents_count() -> None:
    document = inventory_document()
    names = [tool["name"] for tool in document["tools"]]
    assert len(names) == len(set(names))
    assert len(names) == len(TOOL_CAPABILITIES)
    assert len(names) >= 28
    assert names == [cap.name for cap in TOOL_CAPABILITIES]
    # Every tool points at a real handbook document.
    known_docs = {entry.filename for entry in HANDBOOK}
    for tool in document["tools"]:
        assert tool["doc"] in known_docs, tool
    for endpoint in document["endpoints"]:
        assert endpoint["doc"] in known_docs, endpoint


def test_audit_finds_missing_route_and_undeclared_tool() -> None:
    # A route removed from the live app and a tool removed from the registry
    # must both surface; otherwise the parity check would be vacuous.
    incomplete = {"/health": {"get": {}}}
    audit = audit_capabilities(
        openapi_paths=incomplete,
        registered_tool_names=["get_task", "invented_tool"],
    )
    codes = {finding.code for finding in audit.findings}
    assert "unsupported-route" in codes
    assert "undeclared-tool" in codes
    assert "missing-tool" in codes
    assert not audit.ok


def test_local_tools_grant_no_backend_action() -> None:
    # archive_agent applies in-process from a session receipt; it must never
    # widen a profile's raw-HTTP authority to agents.write.
    assert actions_for_tools(["archive_agent"]) == frozenset()
    assert actions_for_tools(["read", "bash", "write_knowledge"]) == frozenset()
    # Real HTTP-backed tools keep their action.
    assert actions_for_tools(["get_task"]) == frozenset({"tasks.read"})
    # Deletion goes through DELETE /knowledge/{path}, so it carries the same
    # write authority as the API route rather than bypassing authorization.
    assert actions_for_tools(["delete_knowledge"]) == frozenset(
        {"knowledge.write"}
    )


def test_permission_mapping_matches_inventory_actions() -> None:
    for cap in TOOL_CAPABILITIES:
        if cap.action is None:
            assert cap.local, cap.name
    assert TOOL_BY_NAME["trigger_review"].action == "review.trigger"
    assert TOOL_BY_NAME["trigger_analysis"].action == "analysis.trigger"
    assert TOOL_BY_NAME["trigger_commit_push"].action == "git.actions"


def test_handbook_discovery_lists_full_docs_but_filters_tools() -> None:
    text = render_handbook_discovery(["get_task", "read", "create_agent"])
    # Full documentation stays discoverable.
    for entry in HANDBOOK:
        assert entry.repo_path in text
        assert entry.sandbox_path in text
    # Only the profile's platform tools are advertised as executable.
    assert "`get_task`" in text
    assert "`create_agent`" in text
    assert "`trigger_review`" not in text
    # A filesystem-only profile is told it has no platform tool.
    none = render_handbook_discovery(["read", "bash"])
    assert "none" in none
    # Without a tool list the executable sentence is omitted entirely.
    assert "Executable platform tools" not in render_handbook_discovery()


def test_executable_tool_names_ignores_unknown_and_deduplicates() -> None:
    assert executable_tool_names(["read", "get_task", "get_task", "bash"]) == (
        "get_task",
    )
    assert executable_tool_names(["root_everything"]) == ()


def test_is_platform_agent_covers_secretary_and_system_agents() -> None:
    assert is_platform_agent("system-code-reviewer", ["read"])
    assert is_platform_agent("system-log-analyzer", [])
    assert is_platform_agent("triage", ["create_task"])
    assert not is_platform_agent("agent-manager-developer", ["read", "bash"])
    assert not is_platform_agent("triage", ["read", "bash"])


def test_write_handbook_discovery_only_for_platform_agents(tmp_path: Path) -> None:
    written = write_handbook_discovery(
        tmp_path, agent_id="system-code-reviewer", tools=["read"]
    )
    assert written is not None and written.is_file()
    assert "docs/handbook/tools-and-api.md" in written.read_text()
    assert (
        write_handbook_discovery(
            tmp_path, agent_id="agent-manager-developer", tools=["read"]
        )
        is None
    )


def test_knowledge_base_syncs_handbook_idempotently(tmp_path: Path) -> None:
    source = tmp_path / "docs" / "handbook"
    source.mkdir(parents=True)
    (source / "README.md").write_text("# Handbook\n", encoding="utf-8")
    (source / "objects.md").write_text("# Objects\n", encoding="utf-8")
    knowledge = KnowledgeBase(tmp_path / "knowledge")
    assert knowledge.sync_handbook(source) == 2
    target = knowledge.root_dir / "handbook"
    assert (target / "README.md").read_text(encoding="utf-8") == "# Handbook\n"
    # A second sync rewrites nothing.
    assert knowledge.sync_handbook(source) == 0
    # A newer source revision replaces stale content.
    (source / "README.md").write_text("# Updated\n", encoding="utf-8")
    assert knowledge.sync_handbook(source) == 1
    assert (target / "README.md").read_text(encoding="utf-8") == "# Updated\n"
    # A missing source is a no-op, not an error.
    assert knowledge.sync_handbook(tmp_path / "absent") == 0


def test_deferred_policy_tools_return_structured_pending(tmp_path: Path) -> None:
    policy = _prepare_policy(tmp_path)
    if policy is None:
        pytest.skip("Node and the Pi TypeBox runtime are required")
    session = tmp_path / "session"
    session.mkdir()
    result = _run_node(
        Path(__file__).with_name("policy_deferred_tools.mjs"),
        str(policy),
        str(session),
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_capability_inventory_covers_every_endpoint_once() -> None:
    keys = [
        (cap.method, cap.path)
        for cap in ENDPOINT_CAPABILITIES
    ]
    assert len(keys) == len(set(keys))
    assert len(CAPABILITIES) == len(TOOL_CAPABILITIES) + len(ENDPOINT_CAPABILITIES)


def test_bootstrap_does_not_widen_customized_secretary_tools(
    tmp_path: Path, migrated_db: Path
) -> None:
    """Re-running bootstrap keeps a customized tool list and its true actions.

    A local-only tool such as ``archive_agent`` must not silently acquire
    ``agents.write`` (which would widen the profile's raw-HTTP authority), and
    bootstrap must not replace a restricted customized list with the full
    default secretary set.
    """
    database = Database.from_path(migrated_db)
    knowledge = KnowledgeBase(tmp_path / "knowledge")
    try:
        repository = AgentProfileRepository(database)
        repository.create(
            agent_id="system-secretary",
            name="Custom Secretary",
            role="secretary",
            system_prompt="Custom identity must remain.",
            runner_config={"tools": ["get_task", "archive_agent"]},
        )
        knowledge.ensure_default_profiles(database)
        stored = repository.get_current("system-secretary")
        assert stored is not None
        assert stored.system_prompt == "Custom identity must remain."
        assert set(stored.runner_config["tools"]) == {
            "get_task",
            "archive_agent",
        }
        assert "create_agent" not in stored.runner_config["tools"]
        assert actions_for_tools(stored.runner_config["tools"]) == frozenset(
            {"tasks.read"}
        )
    finally:
        database.dispose()
