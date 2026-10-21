"""Completion events use the real durable secretary queue and delivery state."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from kuzya.api import create_app
from kuzya.dispatcher import QueueDispatcher
from kuzya.permissions import ExecutionPermissions
from kuzya.secretary import TurnProcessor
from kuzya.storage import ConversationRepository, Database, TaskRepository
from kuzya.task_subscriptions import TaskSubscriptions
from kuzya.telegram.keyboards import dialog_switch_keyboard


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
        bot.send_rich_message.return_value.message_id = 501
        assert asyncio.run(restarted.deliver_replies(bot)) == 1
        assert asyncio.run(restarted.deliver_replies(bot)) == 0
        assert restarted.enqueue_ready() == 0
        # Subscription replies now flow through the unified messenger: a task
        # result header, the full conversation UUID footer and the explicit
        # context-switch button, instead of a bare parse_mode=None text. The
        # delivery guarantee under test is unchanged: one send, no secretary
        # re-run after restart. The button targets the conversation the result
        # arrived in; this dialog has no task binding of its own.
        bot.send_rich_message.assert_awaited_once()
        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs["chat_id"] == 123
        # The Markdown transport wraps the footer hashtag in an inline code
        # span so Telegram renders it small instead of as a heading.
        assert kwargs["rich_message"].markdown == (
            f"📦 Результат задачи №{task.number} «Research»\n\n"
            "Research is ready: Found the result\n\n"
            f"`#{conv.id}`"
        )
        assert kwargs["reply_markup"] == dialog_switch_keyboard(
            conversation_id=conv.id
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
        # The unified sender falls back rich → plain → document, so a full
        # transport outage must fail every transport before the retry can
        # prove that delivery resumes without re-running the secretary.
        transports = (bot.send_rich_message, bot.send_message, bot.send_document)
        for method in transports:
            method.side_effect = RuntimeError("transport unavailable")
        with pytest.raises(RuntimeError):
            asyncio.run(service.deliver_replies(bot))
        for method in transports:
            method.side_effect = None
        bot.send_rich_message.return_value.message_id = 601
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


def test_subscription_button_switches_to_subscribed_conversation(migrated_db):
    """The result button must lead into the conversation the result arrived in.

    Regression: the button used the finished task's own dialog (creating one
    when absent), so pressing it from dialog A jumped the user into an
    unrelated dialog B instead of continuing A.
    """
    db = Database.from_path(migrated_db)
    try:
        repo, task, conversations, conv_a = setup_subscription(db)
        # The finished task belongs to its own, different dialog B…
        conv_b = conversations.create_conversation(title="Task dialog")
        repo.update(task.id, metadata={"conversation_id": conv_b.id})
        # …while dialog A is where the user subscribed and receives the result.
        service = TaskSubscriptions(db)
        service.subscribe(task.id, conv_a.id)
        repo.complete_execution(task.id, status="done", summary="Ready")
        service.enqueue_ready()
        QueueDispatcher(db, handler=lambda turn_input: "Finished").step()

        bot = AsyncMock()
        bot.send_rich_message.return_value.message_id = 701
        assert asyncio.run(TaskSubscriptions(db).deliver_replies(bot)) == 1
        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs["reply_markup"] == dialog_switch_keyboard(
            conversation_id=conv_a.id
        )
        assert kwargs["rich_message"].markdown.endswith(f"`#{conv_a.id}`")
    finally:
        db.dispose()


def test_subscription_button_uses_conversations_own_task_binding(migrated_db):
    """When dialog A itself belongs to a task, its button keeps that binding."""
    db = Database.from_path(migrated_db)
    try:
        repo, task, conversations, conv_a = setup_subscription(db)
        owner_task = repo.create(
            title="Диалог A задача", status="in_progress",
            metadata={"conversation_id": conv_a.id},
        )
        service = TaskSubscriptions(db)
        service.subscribe(task.id, conv_a.id)
        repo.complete_execution(task.id, status="done", summary="Ready")
        service.enqueue_ready()
        QueueDispatcher(db, handler=lambda turn_input: "Finished").step()

        bot = AsyncMock()
        bot.send_rich_message.return_value.message_id = 702
        assert asyncio.run(TaskSubscriptions(db).deliver_replies(bot)) == 1
        kwargs = bot.send_rich_message.await_args.kwargs
        assert kwargs["reply_markup"] == dialog_switch_keyboard(
            conversation_id=conv_a.id,
            task_id=owner_task.id,
            task_number=owner_task.number,
        )
    finally:
        db.dispose()
