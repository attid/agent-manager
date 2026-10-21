from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import FSInputFile

from kuzya.knowledge import (
    DEFAULT_KNOWLEDGE_DIR,
    KnowledgeBase,
    KnowledgeDocumentNotFoundError,
    KnowledgePathError,
    KnowledgeProtectedPathError,
    contained_target_path,
    format_agent_profile_markdown,
)
from kuzya.storage import (
    AgentProfile,
    AgentProfileRepository,
    Database,
)
from kuzya.telegram.state import UserStateStorage
from kuzya.telegram.transcription import DeepgramTranscriber
from kuzya.telegram.worker import QueueProcessor


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

    assert (tmp_path / "knowledge/projects/kuzya/summary.md").is_file()
    assert (tmp_path / "knowledge/components/aiogram/rules.md").is_file()
    assert (tmp_path / "knowledge/components/bubblewrap/rules.md").is_file()

    summary = (tmp_path / "knowledge/projects/kuzya/summary.md").read_text(
        encoding="utf-8"
    )
    assert "Kuzya" in summary


def test_knowledge_base_sync_agents(tmp_path: Path, migrated_db: Path) -> None:
    db = Database.from_path(migrated_db)
    repo = AgentProfileRepository(db)
    repo.create(
        agent_id="kuzya-secretary",
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
    assert "delete_knowledge" in content
    assert 'agent_id: "system-secretary"' in content

    dev_file = tmp_path / "knowledge/agents/kuzya-developer.md"
    assert dev_file.is_file()


def test_knowledge_base_resolve_file(tmp_path: Path) -> None:
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize()

    # 1. Project shorthand
    res_proj = kb.resolve_file("kuzya")
    assert res_proj is not None
    assert res_proj.name == "summary.md"
    assert "projects/kuzya" in str(res_proj)

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
        agent_md = kb_dir / "agents/kuzya-developer.md"
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
                    "text": "Вот профиль разработчика:\n[SEND_FILE: kuzya-developer]\nОзнакомьтесь.",
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

        # Verify send_document was called; the caption carries the dialog
        # UUID footer so the attachment stays tied to its conversation.
        assert bot_mock.send_document.called
        doc_args = bot_mock.send_document.call_args[1]
        assert doc_args["chat_id"] == 100
        assert isinstance(doc_args["document"], FSInputFile)
        assert Path(doc_args["document"].path) == agent_md
        assert doc_args["caption"] == "📄 kuzya-developer.md\n\n#conv-001"

    asyncio.run(_run())


def test_knowledge_base_hot_reload_agent(tmp_path: Path, migrated_db: Path) -> None:
    db = Database.from_path(migrated_db)
    repo = AgentProfileRepository(db)
    repo.create(
        agent_id="kuzya-developer",
        name="Developer",
        role="developer",
        system_prompt="Initial instructions.",
        runner_config={"tools": ["read"]},
    )

    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize(database=db)

    agent_file = tmp_path / "knowledge/agents/kuzya-developer.md"
    assert agent_file.is_file()

    # No changes yet -> sync returns False
    assert not kb.sync_agent_from_file("kuzya-developer", db)
    assert repo.get_current("kuzya-developer").revision == 1

    # Modify markdown file on disk
    updated_md = """---
agent_id: "kuzya-developer"
name: "Developer"
role: "developer"
tools: ["read", "write"]
---

## System Instructions

Updated new instructions for developer.
"""
    agent_file.write_text(updated_md, encoding="utf-8")

    # Hot reload triggered
    assert kb.sync_agent_from_file("kuzya-developer", db)
    updated_profile = repo.get_current("kuzya-developer")
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
    assert "kuzya" in catalog
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
    for target in ("../../escape", "/tmp/kuzya-escape"):
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


def test_delete_document_removes_only_one_exact_markdown_file(
    tmp_path: Path,
) -> None:
    """The Cyrillic filename is deleted without touching its Latin sibling."""
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize()

    cyrillic = kb.root_dir / "инструкции/секретарь.md"
    latin = kb.root_dir / "instructions/secretary.md"
    cyrillic.parent.mkdir(parents=True)
    latin.parent.mkdir(parents=True)
    cyrillic.write_text("# Секретарь\n", encoding="utf-8")
    latin.write_text("# Secretary\n", encoding="utf-8")

    deleted = kb.delete_document("инструкции/секретарь.md")

    assert deleted == cyrillic.resolve()
    assert not cyrillic.exists()
    assert latin.is_file()
    assert latin.read_text(encoding="utf-8") == "# Secretary\n"

    with pytest.raises(KnowledgeDocumentNotFoundError):
        kb.delete_document("инструкции/секретарь.md")


def test_delete_document_rejects_agents_profiles(tmp_path: Path) -> None:
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize()
    profile = kb.agents_dir / "custom-worker.md"
    profile.write_text("# Profile\n", encoding="utf-8")

    with pytest.raises(KnowledgeProtectedPathError):
        kb.delete_document("agents/custom-worker.md")

    assert profile.is_file()


@pytest.mark.parametrize(
    "path",
    [
        "",
        "instructions",
        "instructions/",
        "instructions/secretary.txt",
        "instructions/../secretary.md",
        "./instructions/secretary.md",
        "instructions//secretary.md",
        "instructions/*.md",
        "instructions/secretary?.md",
        "instructions/[ab].md",
        "/instructions/secretary.md",
        r"instructions\secretary.md",
    ],
)
def test_delete_document_rejects_non_explicit_paths(
    tmp_path: Path, path: str
) -> None:
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize()
    target = kb.root_dir / "instructions/secretary.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Secretary\n", encoding="utf-8")

    with pytest.raises(KnowledgePathError):
        kb.delete_document(path)

    assert target.is_file()


def test_delete_document_rejects_symlink_escape(tmp_path: Path) -> None:
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.md"
    outside_file.write_text("# Outside\n", encoding="utf-8")

    file_link = kb.root_dir / "linked.md"
    file_link.symlink_to(outside_file)
    with pytest.raises(KnowledgePathError):
        kb.delete_document("linked.md")
    assert outside_file.is_file()
    assert file_link.is_symlink()

    directory_link = kb.root_dir / "linked-dir"
    directory_link.symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(KnowledgePathError):
        kb.delete_document("linked-dir/secret.md")
    assert outside_file.is_file()


def test_delete_document_does_not_delete_directories_or_globs(
    tmp_path: Path,
) -> None:
    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize()
    docs = kb.root_dir / "docs"
    docs.mkdir()
    first = docs / "one.md"
    second = docs / "two.md"
    first.write_text("# One\n", encoding="utf-8")
    second.write_text("# Two\n", encoding="utf-8")
    nested_dir = docs / "folder.md"
    nested_dir.mkdir()

    for path in ("docs", "docs/", "docs/*", "docs/*.md", "docs/folder.md"):
        with pytest.raises(KnowledgePathError):
            kb.delete_document(path)

    assert docs.is_dir()
    assert nested_dir.is_dir()
    assert first.is_file()
    assert second.is_file()


def test_delete_document_commits_removal_to_knowledge_repository(
    tmp_path: Path,
) -> None:
    import subprocess

    kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
    kb.initialize()
    cyrillic = kb.root_dir / "инструкции/секретарь.md"
    latin = kb.root_dir / "instructions/secretary.md"
    cyrillic.parent.mkdir(parents=True)
    latin.parent.mkdir(parents=True)
    cyrillic.write_text("# Секретарь\n", encoding="utf-8")
    latin.write_text("# Secretary\n", encoding="utf-8")

    subprocess.run(
        ["git", "init"], cwd=str(kb.root_dir), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Knowledge Test"],
        cwd=str(kb.root_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "knowledge@test.local"],
        cwd=str(kb.root_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=str(kb.root_dir), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial knowledge"],
        cwd=str(kb.root_dir),
        check=True,
        capture_output=True,
    )

    kb.delete_document("инструкции/секретарь.md")

    subject = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(kb.root_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert subject == "Delete knowledge document: инструкции/секретарь.md"
    changed = subprocess.run(
        ["git", "-c", "core.quotePath=false", "show", "--name-status", "--format="],
        cwd=str(kb.root_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "D\tинструкции/секретарь.md" in changed
    assert "instructions/secretary.md" not in changed
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(kb.root_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""
    assert latin.is_file()


def test_default_reviewer_engine_is_agy_and_explicit_choice_is_preserved(
    tmp_path: Path, migrated_db: Path
) -> None:
    db = Database.from_path(migrated_db)
    try:
        kb = KnowledgeBase(root_dir=tmp_path / "knowledge")
        kb.ensure_default_profiles(db)
        repo = AgentProfileRepository(db)

        reviewer = repo.get_current("system-code-reviewer")
        assert reviewer is not None
        assert reviewer.runner_config["engine"] == "agy"
        assert reviewer.runner_config["effort"] == "high"

        exported = (
            tmp_path / "knowledge" / "agents" / "system-code-reviewer.md"
        )
        kb.save_agent_profile(reviewer)
        content = exported.read_text(encoding="utf-8")
        assert '"engine": "agy"' in content
        assert '"effort": "high"' in content

        # An explicitly selected engine survives repeated bootstrap so the
        # reviewer can be switched back to the standard Pi runner.
        switched = dict(reviewer.runner_config)
        switched["engine"] = "pi"
        repo.update(
            agent_id="system-code-reviewer",
            expected_revision=reviewer.revision,
            runner_config=switched,
        )
        kb.ensure_default_profiles(db)
        current = repo.get_current("system-code-reviewer")
        assert current is not None
        assert current.runner_config["engine"] == "pi"
    finally:
        db.dispose()
