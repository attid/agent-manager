"""Completion events use the real durable secretary queue and delivery state."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from agent_manager.api import create_app
from agent_manager.dispatcher import QueueDispatcher
from agent_manager.permissions import ExecutionPermissions
from agent_manager.secretary import TurnProcessor
from agent_manager.storage import ConversationRepository, Database, TaskRepository
from agent_manager.task_subscriptions import TaskSubscriptions


def setup_subscription(db):
    repo = TaskRepository(db)
    task = repo.create(title="Research", status="in_progress")
    conversations = ConversationRepository(db)
    conv = conversations.create_conversation(title="Original dialogue", metadata={"chat_id": 123})
    return repo, task, conversations, conv


def test_completion_resumes_same_dialogue_once_and_delivers_after_restart(migrated_db):
    db = Database.from_path(migrated_db)
    try:
        repo, task, conversations, conv = setup_subscription(db)
        ordinary = conversations.append_message(conversation_id=conv.id, role="user", text="Earlier message")
        TurnProcessor(db).enqueue(conversation_id=conv.id, input_message_id=ordinary.message.id)
        service = TaskSubscriptions(db)
        assert service.subscribe(str(task.number), conv.id)["status"] == "pending"
        service.subscribe(task.id, conv.id)
        assert service.enqueue_ready() == 0
        repo.complete_execution(task.id, status="done", summary="Found the result")
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(lambda _: TaskSubscriptions(db).enqueue_ready(), range(2))) == 1
        seen = []

        def secretary(turn_input):
            seen.append(turn_input)
            if turn_input.text == "Earlier message":
                return "Normal reply"
            assert turn_input.conversation_id == conv.id
            assert turn_input.metadata["task_event"]["task_id"] == task.id
            assert "не одобрение пользователя" in turn_input.text
            return "Research is ready: " + turn_input.metadata["task_event"]["summary"]

        dispatcher = QueueDispatcher(db, handler=secretary)
        assert dispatcher.step().reply.text == "Normal reply"
        assert dispatcher.step().reply.text == "Research is ready: Found the result"
        assert dispatcher.step() is None
        restarted = TaskSubscriptions(db)
        bot = AsyncMock()
        assert asyncio.run(restarted.deliver_replies(bot)) == 1
        assert asyncio.run(restarted.deliver_replies(bot)) == 0
        assert restarted.enqueue_ready() == 0
        bot.send_message.assert_awaited_once_with(
            chat_id=123, text="Research is ready: Found the result", parse_mode=None,
        )
        assert len(seen) == 2
    finally:
        db.dispose()


def test_failed_task_and_cancelled_subscription(migrated_db):
    db = Database.from_path(migrated_db)
    try:
        repo, task, conversations, conv = setup_subscription(db)
        service = TaskSubscriptions(db)
        service.subscribe(task.id, conv.id)
        assert service.cancel(task.id, conv.id)["status"] == "cancelled"
        repo.complete_execution(task.id, status="failed", error="Runner failed")
        assert service.enqueue_ready() == 0
        service.subscribe(task.id, conv.id)
        assert service.enqueue_ready() == 1
        with pytest.raises(ValueError, match="already queued"):
            service.cancel(task.id, conv.id)
        messages = conversations.list_messages(conv.id)
        assert messages[0].source == "task_event"
        assert messages[0].meta["task_event"]["error"] == "Runner failed"
    finally:
        db.dispose()


def test_delivery_failure_retries_reply_without_rerunning_secretary(migrated_db):
    db = Database.from_path(migrated_db)
    try:
        repo, task, _, conv = setup_subscription(db)
        service = TaskSubscriptions(db)
        service.subscribe(task.id, conv.id)
        repo.complete_execution(task.id, status="done", summary="Ready")
        service.enqueue_ready()
        handler = lambda turn_input: "Finished"
        dispatcher = QueueDispatcher(db, handler=handler)
        assert dispatcher.step().status == "completed"
        bot = AsyncMock()
        bot.send_message.side_effect = [RuntimeError("transport unavailable"), None]
        with pytest.raises(RuntimeError):
            asyncio.run(service.deliver_replies(bot))
        assert asyncio.run(TaskSubscriptions(db).deliver_replies(bot)) == 1
        assert dispatcher.step() is None
        assert service.enqueue_ready() == 0
    finally:
        db.dispose()


def test_subscription_api_confines_execution_to_its_original_conversation(migrated_db):
    db = Database.from_path(migrated_db)
    permissions = ExecutionPermissions()
    try:
        _, task, conversations, conv = setup_subscription(db)
        other = conversations.create_conversation(title="Other")
        grant = permissions.issue(agent_id="secretary", label=f"conversation:{conv.id}",
                                  actions={"tasks.write"}, resources={"*"})
        reader = permissions.issue(agent_id="reader", label=f"conversation:{conv.id}",
                                   actions={"tasks.read"}, resources={"*"})
        app = create_app(f"sqlite:///{migrated_db}", permissions=permissions)
        with TestClient(app) as client:
            route = f"/tasks/{task.number}/subscriptions"
            own = {"conversation_id": conv.id}
            headers = {"Authorization": f"Bearer {grant.token}"}
            assert client.post(route, json=own).status_code == 401
            assert client.post(route, json=own, headers={"Authorization": f"Bearer {reader.token}"}).status_code == 403
            assert client.post(route, json={"conversation_id": other.id}, headers=headers).status_code == 403
            response = client.post(route, json=own, headers=headers)
            assert response.status_code == 200
            assert response.json()["task_id"] == task.id
            assert client.request("DELETE", route, json=own, headers=headers).json()["status"] == "cancelled"
    finally:
        db.dispose()
