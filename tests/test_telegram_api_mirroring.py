"""API conversation mirroring: both question paths create identical deliveries.

``/ask`` and ``messages`` → ``turns`` must produce the same durable Telegram
delivery rows (question copy, reply), stamp the server-verified caller rather
than client metadata, and stay idempotent on replayed requests.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from agent_manager.api import create_app
from agent_manager.permissions import ExecutionPermissions
from agent_manager.secretary import TurnProcessor
from agent_manager.storage import ConversationRepository, Database
from agent_manager.storage.telegram_delivery import (
    DELIVERY_KIND_API_QUESTION,
    DELIVERY_KIND_REPLY,
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENT,
    TelegramDeliveryRepository,
)
from agent_manager.telegram.messenger import TelegramDeliveryService


def test_ask_stamps_verified_caller_and_enqueues_question_copy(migrated_db) -> None:
    permissions = ExecutionPermissions()
    grant = permissions.issue(
        agent_id="agy",
        label="build",
        actions={"conversations.write", "turns.write"},
        resources={"*"},
    )
    app = create_app(f"sqlite:///{migrated_db}", permissions=permissions)
    database = Database.from_path(migrated_db)
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {grant.token}"}
            response = client.post(
                "/ask",
                json={
                    "message": "Статус сборки?",
                    "timeout_seconds": 0.1,
                },
                headers=headers,
            )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "timeout"  # no dispatcher in this test

        conversations = ConversationRepository(database)
        messages = conversations.list_messages(body["conversation_id"])
        # The verified caller rides only on the delivery row; the stored
        # message keeps exactly the client-supplied metadata.
        assert messages[0].meta == {}

        repo = TelegramDeliveryRepository(database)
        rows = repo.list_deliveries(conversation_id=body["conversation_id"])
        assert [row.kind for row in rows] == [DELIVERY_KIND_API_QUESTION]
        assert rows[0].status == DELIVERY_STATUS_PENDING
        assert rows[0].meta["api_caller"] == "execution:agy|build"
        assert rows[0].meta["input_source"] == "api"
    finally:
        database.dispose()


def test_messages_turns_path_creates_same_deliveries_as_ask(migrated_db) -> None:
    app = create_app(f"sqlite:///{migrated_db}", api_tokens={"admin-token"})
    database = Database.from_path(migrated_db)
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer admin-token"}
            conv = client.post(
                "/conversations", json={}, headers=headers
            ).json()

            first = client.post(
                f"/conversations/{conv['id']}/messages",
                json={"text": "Вопрос один", "role": "user"},
                headers=headers,
            )
            assert first.status_code == 201
            # Idempotent replay: same external_id, same payload → 200, no new
            # message and no second Telegram copy.
            replay = client.post(
                f"/conversations/{conv['id']}/messages",
                json={
                    "text": "Вопрос один",
                    "role": "user",
                    "external_id": "q1",
                },
                headers=headers,
            )
            assert replay.status_code == 201
            replayed = client.post(
                f"/conversations/{conv['id']}/messages",
                json={
                    "text": "Вопрос один",
                    "role": "user",
                    "external_id": "q1",
                },
                headers=headers,
            )
            assert replayed.status_code == 200

            turn = client.post(
                f"/conversations/{conv['id']}/turns",
                json={"input_message_id": replay.json()["id"]},
                headers=headers,
            ).json()
            # Replaying the turn enqueue returns the same turn (created=False).
            replay_turn = client.post(
                f"/conversations/{conv['id']}/turns",
                json={"input_message_id": replay.json()["id"]},
                headers=headers,
            ).json()
            assert replay_turn["id"] == turn["id"]

        # Complete the turn out-of-band, as the dispatcher would.
        processor = TurnProcessor(database)
        claimed = processor.claim_next(conversation_id=conv["id"])
        assert claimed is not None and claimed.id == turn["id"]
        processor.complete(claimed.id, claimed.claim_token, text="Ответ")

        repo = TelegramDeliveryRepository(database)
        rows = repo.list_deliveries(conversation_id=conv["id"])
        # Two inserted questions ("first" without a key, "q1" with one — its
        # replay inserted nothing) and one reply row; every question row
        # precedes the reply so Telegram always sees the question first.
        assert [row.kind for row in rows] == [
            DELIVERY_KIND_API_QUESTION,
            DELIVERY_KIND_API_QUESTION,
            DELIVERY_KIND_REPLY,
        ]
        reply_row = rows[-1]
        assert all(row.sequence < reply_row.sequence for row in rows[:-1])
        assert reply_row.meta["input_source"] == "api"
    finally:
        database.dispose()


def test_end_to_end_question_copy_before_reply_with_uuid_and_links(migrated_db):
    """The original scenario: an API dialog mirrored into the owner's chat.

    The question copy reaches Telegram before the reply, both carry the same
    full conversation UUID, and every sent message registers a durable link
    that the Reply-routing endpoint can resolve back to the dialog.
    """
    app = create_app(f"sqlite:///{migrated_db}", api_tokens={"admin-token"})
    database = Database.from_path(migrated_db)
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer admin-token"}
            conv = client.post(
                "/conversations",
                json={"metadata": {"chat_id": 77}},
                headers=headers,
            ).json()
            message = client.post(
                f"/conversations/{conv['id']}/messages",
                json={"text": "Что с релизом?", "role": "user"},
                headers=headers,
            ).json()
            client.post(
                f"/conversations/{conv['id']}/turns",
                json={"input_message_id": message["id"]},
                headers=headers,
            )

        processor = TurnProcessor(database)
        claimed = processor.claim_next(conversation_id=conv["id"])
        processor.complete(claimed.id, claimed.claim_token, text="Релиз готов")

        bot = AsyncMock()
        bot.send_rich_message.side_effect = [
            SimpleNamespace(message_id=501),
            SimpleNamespace(message_id=502),
        ]
        service = TelegramDeliveryService(
            bot=bot,
            database=database,
            claim_grace_seconds=0.0,
        )
        assert asyncio.run(service.deliver_pending()) == 2

        sends = bot.send_rich_message.await_args_list
        assert len(sends) == 2
        question_markdown = sends[0].kwargs["rich_message"].markdown
        reply_markdown = sends[1].kwargs["rich_message"].markdown
        assert question_markdown.startswith("📨 Вопрос секретарю через API")
        assert "Что с релизом?" in question_markdown
        assert question_markdown.endswith(f"#{conv['id']}")
        assert reply_markdown.startswith("💬 Ответ секретаря (вопрос был через API)")
        assert reply_markdown.endswith(f"#{conv['id']}")

        repo = TelegramDeliveryRepository(database)
        rows = repo.list_deliveries(conversation_id=conv["id"])
        assert [row.kind for row in rows] == [
            DELIVERY_KIND_API_QUESTION,
            DELIVERY_KIND_REPLY,
        ]
        assert all(row.status == DELIVERY_STATUS_SENT for row in rows)

        # Every sent Telegram message resolves back to the dialog, so a Reply
        # to either part routes into the original conversation.
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer admin-token"}
            for telegram_message_id in (501, 502):
                response = client.get(
                    f"/telegram/links/77/{telegram_message_id}", headers=headers
                )
                assert response.status_code == 200
                assert response.json()["conversation_id"] == conv["id"]
                assert response.json()["origin"] == "bot"
            foreign = client.get(
                "/telegram/links/77/999999", headers=headers
            )
            assert foreign.status_code == 404
    finally:
        database.dispose()
