"""Active direct-agent talks serialize their agent workspace with tasks."""

from __future__ import annotations

import pathlib

import pytest

from kuzya.secretary import TurnConversationClosedError, TurnProcessor
from kuzya.storage import (
    AgentProfileRepository,
    ConversationRepository,
    Database,
    TaskRepository,
    WorkspaceBusyError,
    WorkspaceTalkRepository,
)


def _profile(
    database: Database,
    agent_id: str,
    *,
    project: str,
    talkable: bool = False,
) -> None:
    AgentProfileRepository(database).create(
        agent_id=agent_id,
        name=agent_id,
        role="developer",
        system_prompt="test profile",
        runner_config={
            "project": project,
            "talkable": talkable,
        },
    )


def _active_talk(
    repository: ConversationRepository,
    *,
    conversation_id: str = "conv-talk",
    agent_id: str = "devops-docker3",
    workspace: str | None = "ws-devops",
) -> None:
    metadata = {
        "origin": "telegram_agent_talk",
        "talkable_agent": agent_id,
        "talk_active": True,
    }
    if workspace is not None:
        metadata["talk_workspace"] = workspace
    repository.create_conversation(
        id=conversation_id, metadata=metadata
    )


def test_active_talk_blocks_same_workspace_task_until_ended(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(database, "devops-docker3", project="ws-devops")
        conversations = ConversationRepository(database)
        _active_talk(conversations)
        tasks = TaskRepository(database)
        task = tasks.create(
            title="Deploy nginx",
            status="in_progress",
            workspace="ws-devops",
        )

        assert tasks.claim_in_progress(worker_id="worker-1") is None

        conversations.update_conversation(
            "conv-talk", metadata={"talk_active": False}
        )
        claimed = tasks.claim_in_progress(worker_id="worker-1")
        assert claimed is not None
        assert claimed.id == task.id
    finally:
        database.dispose()


def test_active_talk_blocks_tasks_of_the_same_agent_in_another_workspace(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(database, "devops-docker3", project="ws-devops")
        _active_talk(ConversationRepository(database))
        tasks = TaskRepository(database)
        tasks.create(
            title="Other workspace",
            status="in_progress",
            workspace="ws-other",
            agent_id="devops-docker3",
        )

        assert (
            tasks.claim_in_progress(worker_id="worker-1") is None
        )
    finally:
        database.dispose()


def test_active_talk_blocks_default_agent_project_resolution(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(database, "kuzya-developer", project="ws-default")
        # The talk belongs to another agent but occupies the same project
        # directory that a task without an explicit workspace would resolve.
        _active_talk(
            ConversationRepository(database),
            agent_id="devops-docker3",
            workspace="ws-default",
        )
        tasks = TaskRepository(database)
        tasks.create(title="Default workspace task", status="in_progress")

        assert (
            tasks.claim_in_progress(
                worker_id="worker-1",
                default_agent_id="kuzya-developer",
            )
            is None
        )
    finally:
        database.dispose()


def test_talk_start_waits_for_a_running_task_and_acquires_after_it(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(database, "devops-docker3", project="ws-devops")
        conversations = ConversationRepository(database)
        conversations.create_conversation(
            id="conv-talk",
            metadata={
                "origin": "telegram_agent_talk",
                "talkable_agent": "devops-docker3",
                "talk_workspace": "ws-devops",
            },
        )
        tasks = TaskRepository(database)
        tasks.create(
            title="Running deploy",
            status="in_progress",
            workspace="ws-devops",
        )
        running = tasks.claim_in_progress(
            worker_id="task-worker", default_agent_id="kuzya-developer"
        )
        assert running is not None

        talks = WorkspaceTalkRepository(database)
        started = talks.start("conv-talk")
        metadata = dict(started.meta)
        assert metadata["talk_active"] is False
        assert metadata["talk_pending"] is True
        with pytest.raises(WorkspaceBusyError):
            talks.acquire_for_turn("conv-talk")

        tasks.complete_execution(running.id, status="done")
        acquired = talks.acquire_for_turn("conv-talk")
        assert acquired.meta["talk_active"] is True
        assert acquired.meta["talk_pending"] is False
    finally:
        database.dispose()


def test_end_waits_for_pending_turn_and_refuses_new_turns(
    migrated_db: pathlib.Path,
) -> None:
    database = Database.from_path(migrated_db)
    try:
        _profile(database, "devops-docker3", project="ws-devops")
        conversations = ConversationRepository(database)
        conversations.create_conversation(
            id="conv-talk",
            metadata={
                "origin": "telegram_agent_talk",
                "talkable_agent": "devops-docker3",
                "talk_workspace": "ws-devops",
            },
        )
        talks = WorkspaceTalkRepository(database)
        assert talks.start("conv-talk").meta["talk_active"] is True

        processor = TurnProcessor(database)
        message = conversations.append_message(
            conversation_id="conv-talk",
            role="user",
            text="проверь nginx",
            source="test",
        ).message
        processor.enqueue(
            conversation_id="conv-talk", input_message_id=message.id
        )

        closing = talks.request_end("conv-talk")
        assert closing.meta["talk_closing"] is True
        assert closing.meta["talk_active"] is True
        assert talks.finalize_if_idle("conv-talk") is False

        later = conversations.append_message(
            conversation_id="conv-talk",
            role="user",
            text="ещё сообщение",
            source="test",
        ).message
        with pytest.raises(TurnConversationClosedError):
            processor.enqueue(
                conversation_id="conv-talk", input_message_id=later.id
            )

        turn = processor.claim_next(conversation_id="conv-talk")
        assert turn is not None
        processor.complete(turn.id, turn.claim_token, text="готово")
        assert talks.finalize_if_idle("conv-talk") is True

        ended = conversations.get_conversation("conv-talk")
        assert ended is not None
        assert ended.meta["talk_active"] is False
        assert ended.meta["talk_closing"] is False
        assert ended.meta["talk_closed"] is True
    finally:
        database.dispose()


def test_default_agent_task_blocks_same_project_talk(
    migrated_db: pathlib.Path,
) -> None:
    """A claimed task without explicit fields records what it really uses."""
    database = Database.from_path(migrated_db)
    try:
        _profile(database, "kuzya-developer", project="ws-default")
        _profile(database, "devops-docker3", project="ws-default")
        tasks = TaskRepository(database)
        tasks.create(title="Default workspace task", status="in_progress")
        running = tasks.claim_in_progress(
            worker_id="task-worker",
            default_agent_id="kuzya-developer",
        )
        assert running is not None
        assert running.meta["execution_agent_id"] == "kuzya-developer"
        assert running.meta["execution_workspace"] == "ws-default"

        conversations = ConversationRepository(database)
        conversations.create_conversation(
            id="conv-talk",
            metadata={
                "origin": "telegram_agent_talk",
                "talkable_agent": "devops-docker3",
                "talk_workspace": "ws-default",
            },
        )
        talks = WorkspaceTalkRepository(database)
        started = talks.start("conv-talk")
        assert started.meta["talk_active"] is False
        assert started.meta["talk_pending"] is True
        with pytest.raises(WorkspaceBusyError):
            talks.acquire_for_turn("conv-talk")
    finally:
        database.dispose()


def test_enqueue_reads_closing_state_inside_insert_transaction(
    migrated_db: pathlib.Path, monkeypatch
) -> None:
    """A separate pre-check cannot let a turn slip past a concurrent close."""
    database = Database.from_path(migrated_db)
    try:
        _profile(database, "devops-docker3", project="ws-devops")
        conversations = ConversationRepository(database)
        conversations.create_conversation(
            id="conv-talk",
            metadata={
                "origin": "telegram_agent_talk",
                "talkable_agent": "devops-docker3",
                "talk_workspace": "ws-devops",
            },
        )
        talks = WorkspaceTalkRepository(database)
        talks.start("conv-talk")
        talks.request_end("conv-talk")
        message = conversations.append_message(
            conversation_id="conv-talk",
            role="user",
            text="запоздалый ход",
            source="test",
        ).message

        # The old implementation delegated this decision to a separate
        # repository call. Forcing that call to lie must not create a turn:
        # the insert transaction itself reads the committed closing state.
        monkeypatch.setattr(
            WorkspaceTalkRepository,
            "is_closing",
            lambda _self, _conversation_id: False,
        )
        with pytest.raises(TurnConversationClosedError):
            TurnProcessor(database).enqueue(
                conversation_id="conv-talk",
                input_message_id=message.id,
            )
    finally:
        database.dispose()
