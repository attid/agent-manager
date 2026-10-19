from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import FSInputFile

from agent_manager.knowledge import (
    DEFAULT_KNOWLEDGE_DIR,
    KnowledgeBase,
    KnowledgePathError,
    contained_target_path,
    format_agent_profile_markdown,
)
from agent_manager.storage import (
    AgentProfile,
    AgentProfileRepository,
    Database,
)
from agent_manager.telegram.state import UserStateStorage
from agent_manager.telegram.transcription import DeepgramTranscriber
from agent_manager.telegram.worker import QueueProcessor


def test_format_agent_profile_markdown() -> None:
    profile = AgentProfile(
        agent_id="test-agent",
        revision=1,
        name="Test Agent",
        role="tester",
        system_prompt="You are a test agent.",
        runner_config={
            "provider": "anthropic",
            "model": "claude-3-5-sonnet",
            "tools": ["read_file", "write_file"],
        },
    )
    md = format_agent_profile_markdown(profile)
    assert 'agent_id: "test-agent"' in md
    assert "revision: 1" in md
    assert "provider:" not in md
    assert "model:" not in md
    assert '"read_file", "write_file"' in md
    assert "# Agent Profile: Test Agent" in md
    assert "You are a test agent." in md


def test_knowledge_base_initialize(tmp_path: Path) -> None:
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize()

    assert (tmp_path / "knowledge/projects/agent-manager/summary.md").is_file()
    assert (tmp_path / "knowledge/components/aiogram/rules.md").is_file()
    assert (tmp_path / "knowledge/components/bubblewrap/rules.md").is_file()

    summary = (tmp_path / "knowledge/projects/agent-manager/summary.md").read_text(
        encoding="utf-8"
    )
    assert "Agent Manager" in summary


def test_knowledge_base_sync_agents(tmp_path: Path, migrated_db: Path) -> None:
    db = Database.from_path(migrated_db)
    repo = AgentProfileRepository(db)
    repo.create(
        agent_id="agent-manager-secretary",
        name="Secretary",
        role="secretary",
        system_prompt="Manage tasks and dialogs.",
        runner_config={"model": "gpt-4o", "tools": []},
    )

    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize(database=db)

    exported_file = tmp_path / "knowledge/agents/system-secretary.md"
    assert exported_file.is_file()
    content = exported_file.read_text(encoding="utf-8")
    assert "create_agent" in content
    assert 'agent_id: "system-secretary"' in content

    dev_file = tmp_path / "knowledge/agents/agent-manager-developer.md"
    assert dev_file.is_file()


def test_knowledge_base_resolve_file(tmp_path: Path) -> None:
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize()

    # 1. Project shorthand
    res_proj = kb.resolve_file("agent-manager")
    assert res_proj is not None
    assert res_proj.name == "summary.md"
    assert "projects/agent-manager" in str(res_proj)

    # 2. Component shorthand
    res_comp = kb.resolve_file("aiogram")
    assert res_comp is not None
    assert res_comp.name == "rules.md"
    assert "components/aiogram" in str(res_comp)

    # 3. Relative path
    res_rel = kb.resolve_file("components/bubblewrap/rules.md")
    assert res_rel is not None
    assert res_rel.is_file()

    # 4. Absolute path
    res_abs = kb.resolve_file(str(res_comp))
    assert res_abs == res_comp

    # 5. Non-existent returns None
    assert kb.resolve_file("non-existent-thing") is None


def test_queue_processor_sends_file(tmp_path: Path) -> None:
    async def _run() -> None:
        # Setup knowledge base with starter files
        kb_dir = tmp_path / "knowledge"
        kb = KnowledgeBase(root_dir=kb_dir)
        kb.initialize()

        # Create dummy agent markdown
        agent_md = kb_dir / "agents/agent-manager-developer.md"
        agent_md.write_text("# Developer Profile", encoding="utf-8")

        # Mocks
        bot_mock = AsyncMock()
        bot_mock.edit_message_text = AsyncMock()
        bot_mock.send_message = AsyncMock()
        bot_mock.send_document = AsyncMock()

        client_mock = AsyncMock()
        client_mock.append_message = AsyncMock(return_value={"id": "msg-123"})
        client_mock.create_turn = AsyncMock(return_value={"id": "turn-456"})
        client_mock.get_turn = AsyncMock(
            return_value={"status": "completed", "reply_message_id": "rep-789"}
        )
        client_mock.list_messages = AsyncMock(
            return_value=[
                {
                    "id": "rep-789",
                    "text": "Вот профиль разработчика:\n[SEND_FILE: agent-manager-developer]\nОзнакомьтесь.",
                }
            ]
        )

        storage = UserStateStorage(tmp_path / "state.db")
        queued_msg = storage.enqueue_message(
            user_id=1,
            chat_id=100,
            telegram_message_id=50,
            context_mode="talk",
            context_task_id=None,
            context_conversation_id="conv-001",
            text="Дай мне профиль разработчика",
            status_message_id=51,
        )

        processor = QueueProcessor(
            bot=bot_mock,
            backend_client=client_mock,
            storage=storage,
            transcriber=DeepgramTranscriber(mock_transcript="тест"),
            poll_interval_seconds=0.01,
            turn_timeout_seconds=2.0,
            knowledge_base=kb,
        )

        await processor.process_message(queued_msg)

        # Verify safe_edit_text called with stripped text
        assert bot_mock.edit_message_text.called
        edited_text = bot_mock.edit_message_text.call_args[1]["text"]
        assert "[SEND_FILE:" not in edited_text
        assert "Вот профиль разработчика:" in edited_text

        # Verify send_document was called
        assert bot_mock.send_document.called
        doc_args = bot_mock.send_document.call_args[1]
        assert doc_args["chat_id"] == 100
        assert isinstance(doc_args["document"], FSInputFile)
        assert Path(doc_args["document"].path) == agent_md
        assert doc_args["caption"] == "📄 agent-manager-developer.md"

    asyncio.run(_run())


def test_knowledge_base_hot_reload_agent(tmp_path: Path, migrated_db: Path) -> None:
    db = Database.from_path(migrated_db)
    repo = AgentProfileRepository(db)
    repo.create(
        agent_id="agent-manager-developer",
        name="Developer",
        role="developer",
        system_prompt="Initial instructions.",
        runner_config={"tools": ["read"]},
    )

    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize(database=db)

    agent_file = tmp_path / "knowledge/agents/agent-manager-developer.md"
    assert agent_file.is_file()

    # No changes yet -> sync returns False
    assert not kb.sync_agent_from_file("agent-manager-developer", db)
    assert repo.get_current("agent-manager-developer").revision == 1

    # Modify markdown file on disk
    updated_md = """---
agent_id: "agent-manager-developer"
name: "Developer"
role: "developer"
tools: ["read", "write"]
---

## System Instructions

Updated new instructions for developer.
"""
    agent_file.write_text(updated_md, encoding="utf-8")

    # Hot reload triggered
    assert kb.sync_agent_from_file("agent-manager-developer", db)
    updated_profile = repo.get_current("agent-manager-developer")
    assert updated_profile.revision == 2
    assert "Updated new instructions" in updated_profile.system_prompt
    assert updated_profile.runner_config.get("tools") == ["read", "write"]
    assert "provider" not in updated_profile.runner_config
    assert "model" not in updated_profile.runner_config


def test_knowledge_base_catalog_and_git(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    kb = KnowledgeBase(root_dir=kb_dir)
    kb.initialize()

    # Verify catalog contains projects and components
    catalog = kb.get_catalog_index()
    assert "## Knowledge Base Catalog" in catalog
    assert "agent-manager" in catalog
    assert "aiogram" in catalog
    assert "bubblewrap" in catalog

    # Create/update project summary
    summary_path = kb.create_or_update_project_summary(
        "skynet_bot",
        "# Project: Skynet Bot\n\nTelegram automation bot\n",
    )
    assert summary_path.is_file()
    catalog_updated = kb.get_catalog_index()
    assert "skynet_bot" in catalog_updated


def test_knowledge_base_archive_agent(tmp_path: Path, migrated_db: Path) -> None:
    import subprocess
    db = Database.from_path(migrated_db)
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize(database=db)

    # Initialize a git repo in knowledge base
    subprocess.run(["git", "init"], cwd=str(tmp_path / "knowledge"), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path / "knowledge"), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(tmp_path / "knowledge"), check=True, capture_output=True)

    repo = AgentProfileRepository(db)
    prof = repo.create(
        agent_id="custom-worker",
        name="Custom Worker",
        role="worker",
        system_prompt="Custom prompt",
        runner_config={"tools": ["read"]},
    )
    kb.save_agent_profile(prof)
    agent_file = tmp_path / "knowledge/agents/custom-worker.md"
    assert agent_file.is_file()

    # 1. Test archive_agent method
    assert kb.archive_agent("custom-worker", db)
    assert not agent_file.exists()
    archived_prof = repo.get_current("custom-worker")
    assert archived_prof.runner_config.get("archived") is True
    # Verify sync_agents_from_db does NOT recreate archived agent file
    exported = kb.sync_agents_from_db(db)
    assert "custom-worker" not in exported
    assert not agent_file.exists()


def test_knowledge_base_file_deletion_archives_agent(tmp_path: Path, migrated_db: Path) -> None:
    import subprocess
    db = Database.from_path(migrated_db)
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize(database=db)

    # Initialize git repo in knowledge dir
    subprocess.run(["git", "init"], cwd=str(tmp_path / "knowledge"), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path / "knowledge"), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(tmp_path / "knowledge"), check=True, capture_output=True)

    repo = AgentProfileRepository(db)
    prof = repo.create(
        agent_id="test-delete-agent",
        name="Delete Agent",
        role="worker",
        system_prompt="To be deleted",
        runner_config={"tools": ["read"]},
    )
    kb.save_agent_profile(prof)
    agent_file = tmp_path / "knowledge/agents/test-delete-agent.md"
    assert agent_file.is_file()

    # User deletes the file from disk
    agent_file.unlink()
    assert not agent_file.exists()

    # sync_agents_from_files detects deleted file and archives agent in DB
    imported = kb.sync_agents_from_files(db)
    assert "archived:test-delete-agent" in imported

    current = repo.get_current("test-delete-agent")
    assert current.runner_config.get("archived") is True

    # On subsequent sync from DB, file is not recreated
    exported = kb.sync_agents_from_db(db)
    assert "test-delete-agent" not in exported
    assert not agent_file.exists()





def test_contained_target_path_rejects_escapes_and_allows_nested_names(
    tmp_path: Path,
) -> None:
    kb = KnowledgeBase(tmp_path / "knowledge")
    kb.projects_dir.mkdir(parents=True, exist_ok=True)

    # Broad, nested names are allowed as long as they stay inside the subtree.
    nested = contained_target_path(
        kb.projects_dir, "team/platform", "summary.md"
    )
    assert nested == (
        kb.projects_dir / "team" / "platform" / "summary.md"
    ).resolve()
    assert nested.is_relative_to(kb.projects_dir.resolve())
    kb.create_or_update_project_summary("team/platform", "Nested content.")
    assert nested.read_text(encoding="utf-8").strip() == "Nested content."

    # Traversal and absolute paths are rejected by containment, not by an
    # arbitrary name allowlist.
    for target in ("../../escape", "/tmp/agent-manager-escape"):
        with pytest.raises(KnowledgePathError):
            contained_target_path(kb.projects_dir, target, "summary.md")

    # A symlinked directory that points outside the subtree is rejected too.
    outside = tmp_path / "outside"
    outside.mkdir()
    (kb.projects_dir / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(KnowledgePathError):
        contained_target_path(kb.projects_dir, "linked", "summary.md")

    # The final write uses the same resolver and never creates the file.
    with pytest.raises(KnowledgePathError):
        kb.create_or_update_project_summary("../escape", "must not land")
    assert not (tmp_path / "escape").exists()
