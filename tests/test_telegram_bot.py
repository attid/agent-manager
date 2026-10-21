"""Acceptance and unit tests for the Telegram Secretary adapter."""

from __future__ import annotations

import asyncio
import io
import pathlib
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.types import (
    CallbackQuery,
    Chat,
    File,
    Message,
    PhotoSize,
    Update,
    User,
    Voice,
)

from kuzya.api import create_app
from kuzya.telegram.client import BackendClient
from kuzya.telegram.config import TelegramConfig
from kuzya.telegram.handlers import (
    AuthMiddleware,
    cmd_list,
    cmd_new,
    cmd_start,
    cmd_talk,
    cmd_watch,
    on_category_switch,
    on_select_task,
    on_talk_task,
    on_set_status,
    on_cancel_queue,
    on_user_message,
)
from kuzya.telegram.state import UserStateStorage
from kuzya.telegram.transcription import DeepgramTranscriber
from kuzya.telegram.worker import QueueProcessor
import functools


def sync_test(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapper


def _make_user(user_id: int = 12345, username: str = "testuser") -> User:
    return User(
        id=user_id,
        is_bot=False,
        first_name="Test",
        last_name="User",
        username=username,
    )


def _make_chat(chat_id: int = 12345) -> Chat:
    return Chat(id=chat_id, type="private")


def _make_message(
    text: str = "",
    user: User | None = None,
    chat: Chat | None = None,
    message_id: int = 1,
    voice: Voice | None = None,
    photo: list[PhotoSize] | None = None,
    caption: str | None = None,
    document: Any | None = None,
) -> Message:
    u = user or _make_user()
    c = chat or _make_chat()
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.from_user = u
    msg.chat = c
    msg.text = text
    msg.voice = voice
    msg.photo = photo
    msg.caption = caption
    msg.document = document
    # aiogram Message is a pydantic model, so spec'd mocks cannot see field
    # names; optional fields the handlers rely on are set explicitly.
    msg.reply_to_message = None
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.edit_message_text = AsyncMock()
    msg.bot.send_message = AsyncMock(
        return_value=MagicMock(message_id=999)
    )
    return msg


def _make_callback_query(
    data: str,
    user: User | None = None,
    message: Message | None = None,
) -> CallbackQuery:
    u = user or _make_user()
    msg = message or _make_message(user=u)
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = u
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


from fastapi.testclient import TestClient


@pytest.fixture
def test_backend_client(migrated_db: pathlib.Path):
    app = create_app(f"sqlite:///{migrated_db}")
    with TestClient(app):
        transport = httpx.ASGITransport(app=app)
        http_client = httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        )
        yield BackendClient(base_url="http://testserver", client=http_client)



@pytest.fixture
def user_storage(tmp_path: pathlib.Path) -> UserStateStorage:
    db_file = tmp_path / "telegram_test_state.db"
    return UserStateStorage(db_file)


# 1. Configuration tests


def test_telegram_config_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-xyz")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "99999")
    monkeypatch.setenv("KUZYA_BACKEND_URL", "http://127.0.0.1:18767/")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key-123")
    monkeypatch.setenv("TELEGRAM_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("TELEGRAM_MEDIA_DIR", str(tmp_path / "media"))

    cfg = TelegramConfig.from_env()
    assert cfg.bot_token == "test-token-xyz"
    assert cfg.allowed_user_id == 99999
    assert cfg.backend_url == "http://127.0.0.1:18767"
    assert cfg.deepgram_api_key == "dg-key-123"
    assert cfg.state_db_path == tmp_path / "state.db"
    assert cfg.media_dir == tmp_path / "media"


# 2. SQLite state & queue persistence tests


def test_user_state_and_queue_persistence(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "persistence.db"
    store1 = UserStateStorage(db_path)

    # User state
    store1.set_user_mode(100, "task", task_id="task-1", conversation_id="conv-1")
    store1.set_talk_conversation(100, "conv-talk-100")

    # Queue messages
    q1 = store1.enqueue_message(
        user_id=100,
        chat_id=100,
        telegram_message_id=10,
        context_mode="task",
        context_task_id="task-1",
        context_conversation_id="conv-1",
        text="First message",
    )
    q2 = store1.enqueue_message(
        user_id=100,
        chat_id=100,
        telegram_message_id=11,
        context_mode="task",
        context_task_id="task-1",
        context_conversation_id="conv-1",
        text="Second message",
    )

    # Cancel q2
    assert store1.cancel_queued_message(q2.id) is True
    assert store1.get_queued_message(q2.id).status == "cancelled"

    # Close and reopen store instance
    store2 = UserStateStorage(db_path)
    state = store2.get_user_state(100)
    assert state.mode == "task"
    assert state.active_task_id == "task-1"
    assert state.active_conversation_id == "conv-1"
    assert state.talk_conversation_id == "conv-talk-100"

    # Claim next pending
    claimed = store2.claim_next_pending("conv-1")
    assert claimed is not None
    assert claimed.id == q1.id
    assert claimed.status == "processing"

    # Cancel already processing message fails
    assert store2.cancel_queued_message(q1.id) is False

    # No more pending for conv-1 (q2 was cancelled)
    assert store2.claim_next_pending("conv-1") is None


# 3. Auth middleware test


@sync_test
async def test_auth_middleware() -> None:
    middleware = AuthMiddleware(allowed_user_id=12345)
    handler = AsyncMock(return_value="ok")

    # Allowed user
    msg_allowed = _make_message(user=_make_user(user_id=12345))
    res = await middleware(handler, msg_allowed, {"event_from_user": msg_allowed.from_user})
    assert res == "ok"
    assert handler.call_count == 1

    # Rejected user
    handler.reset_mock()
    msg_rejected = _make_message(user=_make_user(user_id=99999))
    res2 = await middleware(handler, msg_rejected, {"event_from_user": msg_rejected.from_user})
    assert res2 is None
    assert handler.call_count == 0
    msg_rejected.answer.assert_awaited_once_with("⛔️ Доступ запрещен.")


# 4. Commands /start, /new, /talk, /list


@sync_test
async def test_commands_start_new_talk_list(
    user_storage: UserStateStorage,
    test_backend_client: BackendClient,
) -> None:
    user = _make_user(12345)
    msg = _make_message(user=user)

    # /start
    await cmd_start(msg, storage=user_storage)
    msg.answer.assert_awaited_once()
    assert user_storage.get_user_state(user.id).mode == "idle"

    # /new
    msg.answer.reset_mock()
    await cmd_new(msg, storage=user_storage)
    assert user_storage.get_user_state(user.id).mode == "new_task"
    assert "Режим постановки новой задачи" in msg.answer.call_args[0][0]

    # /talk
    msg.answer.reset_mock()
    await cmd_talk(msg, storage=user_storage, client=test_backend_client)
    state = user_storage.get_user_state(user.id)
    assert state.mode == "talk"
    assert state.talk_conversation_id is not None
    assert "Режим общего разговора" in msg.answer.call_args[0][0]

    # Create a task on backend to test /list
    await test_backend_client.create_task(
        title="Тестовая задача в списке",
        status="incoming",
    )
    msg.answer.reset_mock()
    await cmd_list(msg, client=test_backend_client)
    msg.answer.assert_awaited_once()
    assert "Список задач" in msg.answer.call_args[0][0]


# 5. New task creation from first message in new_task mode


@sync_test
async def test_first_message_in_new_task_creates_task_and_conversation(
    user_storage: UserStateStorage,
    test_backend_client: BackendClient,
    tmp_path: pathlib.Path,
) -> None:
    user = _make_user(12345)
    user_storage.set_user_mode(user.id, "new_task")

    msg = _make_message(text="Сделать отчёт по проекту", user=user)
    cfg = TelegramConfig(
        bot_token="fake", allowed_user_id=12345, media_dir=tmp_path
    )

    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock(return_value=MagicMock(message_id=888))
    bot_mock.edit_message_text = AsyncMock()

    processor = QueueProcessor(
        bot=bot_mock,
        backend_client=test_backend_client,
        storage=user_storage,
        transcriber=DeepgramTranscriber(mock_transcript="тест"),
    )

    await on_user_message(
        msg,
        storage=user_storage,
        client=test_backend_client,
        queue_processor=processor,
        config=cfg,
    )

    # Verify task was created on backend
    state = user_storage.get_user_state(user.id)
    assert state.mode == "task"
    assert state.active_task_id is not None
    assert state.active_conversation_id is not None

    task = await test_backend_client.get_task(state.active_task_id)
    assert task["status"] == "incoming"
    assert task["metadata"]["conversation_id"] == state.active_conversation_id


# 6. Task selection and status transitions via callback queries


@sync_test
async def test_task_selection_and_status_patch(
    user_storage: UserStateStorage,
    test_backend_client: BackendClient,
) -> None:
    user = _make_user(12345)
    # Create task on backend
    created = await test_backend_client.create_task(
        title="Задача для переключения статуса",
        description="Подробности",
        status="incoming",
    )
    task_id = created["id"]

    # Select task callback
    cb_select = _make_callback_query(f"select_task:{task_id}", user=user)
    await on_select_task(cb_select, storage=user_storage, client=test_backend_client)
    cb_select.message.edit_text.assert_awaited_once()

    state = user_storage.get_user_state(user.id)
    assert state.mode == "task"
    assert state.active_task_id == task_id
    assert state.active_conversation_id is not None

    # Patch status to in_progress
    cb_status = _make_callback_query(f"set_status:{task_id}:in_progress", user=user)
    await on_set_status(cb_status, client=test_backend_client)
    cb_status.message.edit_text.assert_awaited_once()

    updated = await test_backend_client.get_task(task_id)
    assert updated["status"] == "in_progress"


# 7. FIFO Queue and Cancellation


@sync_test
async def test_watch_command_subscribes_current_dialogue_and_can_cancel(
    user_storage: UserStateStorage,
    test_backend_client: BackendClient,
    migrated_db: pathlib.Path,
) -> None:
    import sqlite3

    task = await test_backend_client.create_task(title="Background research", status="in_progress")
    message = _make_message(text=f"/watch {task['number']} Explain the findings")
    await cmd_watch(message, storage=user_storage, client=test_backend_client)
    state = user_storage.get_user_state(message.from_user.id)
    assert state.active_conversation_id is not None
    assert f"#{task['number']}" in message.answer.call_args.args[0]
    with sqlite3.connect(migrated_db) as connection:
        assert connection.execute(
            "SELECT task_id,conversation_id,status,instructions FROM task_subscriptions"
        ).fetchone() == (task["id"], state.active_conversation_id, "pending", "Explain the findings")
    message.text = f"/unwatch {task['number']}"
    await cmd_watch(message, storage=user_storage, client=test_backend_client)
    assert "отменено" in message.answer.call_args.args[0]
    with sqlite3.connect(migrated_db) as connection:
        assert connection.execute("SELECT status FROM task_subscriptions").fetchone() == ("cancelled",)


@sync_test
async def test_failed_tasks_are_accessible_from_task_list(
    test_backend_client: BackendClient,
) -> None:
    failed = await test_backend_client.create_task(title="Failed research", status="failed")
    await test_backend_client.create_task(title="Active research", status="in_progress")
    message = _make_message(text="/list")
    await cmd_list(message, client=test_backend_client)
    keyboard = message.answer.call_args.kwargs["reply_markup"]
    tabs = [b for row in keyboard.inline_keyboard for b in row]
    assert any(b.callback_data == "cat:failed:1" and "Ошибки" in b.text for b in tabs)

    callback = _make_callback_query("cat:failed:1")
    await on_category_switch(callback, client=test_backend_client)
    keyboard = callback.message.edit_reply_markup.call_args.kwargs["reply_markup"]
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    task_buttons = [b for b in buttons if b.callback_data.startswith("select_task:")]
    assert len(task_buttons) == 1
    assert task_buttons[0].callback_data == f"select_task:{failed['id']}"
    assert f"#{failed['number']}" in task_buttons[0].text


@sync_test
async def test_task_list_tabs_show_live_status_counts(
    test_backend_client: BackendClient,
) -> None:
    await test_backend_client.create_task(title="First incoming", status="incoming")
    await test_backend_client.create_task(title="Second incoming", status="incoming")
    await test_backend_client.create_task(title="Active", status="in_progress")
    await test_backend_client.create_task(title="Old", status="archived")

    message = _make_message(text="/list")
    await cmd_list(message, client=test_backend_client)
    keyboard = message.answer.call_args.kwargs["reply_markup"]
    tabs = {
        button.callback_data.split(":")[1]: button.text
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("cat:")
    }
    assert tabs["incoming"] == "• 📥 Новые 2 •"
    assert tabs["in_progress"] == "⚙️ Работа 1"
    assert tabs["failed"] == "❌ Ошибки 0"
    assert tabs["archived"] == "📦 Архив"

    callback = _make_callback_query("cat:in_progress:1")
    await on_category_switch(callback, client=test_backend_client)
    keyboard = callback.message.edit_reply_markup.call_args.kwargs["reply_markup"]
    tabs = {
        button.callback_data.split(":")[1]: button.text
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("cat:")
    }
    assert tabs["in_progress"] == "• ⚙️ Работа 1 •"
    assert tabs["incoming"] == "📥 Новые 2"


@sync_test
async def test_task_card_escapes_html_from_task_content(
    user_storage: UserStateStorage,
    test_backend_client: BackendClient,
) -> None:
    from html.parser import HTMLParser

    created = await test_backend_client.create_task(
        title="Research <name> & <script>example</script>",
        description="Inspect /tmp/<name> && compare a < b.",
        status="incoming",
    )
    callback = _make_callback_query(f"select_task:{created['id']}")
    await on_select_task(callback, storage=user_storage, client=test_backend_client)
    text = callback.message.edit_text.call_args.args[0]
    assert "Research &lt;name&gt; &amp; &lt;script&gt;example&lt;/script&gt;" in text
    assert "Inspect /tmp/&lt;name&gt; &amp;&amp; compare a &lt; b." in text

    class CardParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag in {"b", "code", "i"}

    CardParser().feed(text)
    callback.answer.assert_awaited_once()
    state = user_storage.get_user_state(callback.from_user.id)
    assert state.active_task_id == created["id"]
    assert state.active_conversation_id is not None

    talk = _make_callback_query(f"talk_task:{created['id']}")
    await on_talk_task(talk, storage=user_storage, client=test_backend_client)
    CardParser().feed(talk.message.answer.call_args.args[0])


@sync_test
async def test_fifo_queue_order_and_cancellation(
    user_storage: UserStateStorage,
    test_backend_client: BackendClient,
) -> None:
    user_id = 12345
    chat_id = 12345
    conv = await test_backend_client.create_conversation(title="Queue test")
    conv_id = conv["id"]

    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock(return_value=MagicMock(message_id=101))
    bot_mock.edit_message_text = AsyncMock()

    processor = QueueProcessor(
        bot=bot_mock,
        backend_client=test_backend_client,
        storage=user_storage,
        transcriber=DeepgramTranscriber(mock_transcript="тест"),
    )

    # 1. Submit message 1 -> conversation becomes busy
    q1 = await processor.submit_message(
        user_id=user_id,
        chat_id=chat_id,
        telegram_message_id=1,
        context_mode="task",
        context_task_id="t1",
        context_conversation_id=conv_id,
        text="Message 1",
    )
    assert q1.status == "pending" or user_storage.is_conversation_busy(conv_id)

    # 2. Submit message 2 while conversation is busy -> placed in queue as pending
    q2 = await processor.submit_message(
        user_id=user_id,
        chat_id=chat_id,
        telegram_message_id=2,
        context_mode="task",
        context_task_id="t1",
        context_conversation_id=conv_id,
        text="Message 2",
    )
    assert q2.status == "pending"

    # 3. Cancel message 2
    cb_cancel = _make_callback_query(f"cancel_queue:{q2.id}", user=_make_user(user_id))
    await on_cancel_queue(cb_cancel, storage=user_storage)
    assert user_storage.get_queued_message(q2.id).status == "cancelled"

    # 4. Submit message 3
    q3 = await processor.submit_message(
        user_id=user_id,
        chat_id=chat_id,
        telegram_message_id=3,
        context_mode="task",
        context_task_id="t1",
        context_conversation_id=conv_id,
        text="Message 3",
    )
    assert q3.status == "pending"

    # 5. After q1 completes, next eligible pending claimed is q3, skipping cancelled q2
    claimed_next = user_storage.claim_next_pending(conv_id)
    assert claimed_next is not None
    assert claimed_next.id == q3.id
    assert claimed_next.status == "processing"


# 8. Voice message with Deepgram transcription


@sync_test
async def test_voice_message_transcription_and_turn(
    user_storage: UserStateStorage,
    test_backend_client: BackendClient,
    tmp_path: pathlib.Path,
) -> None:
    user = _make_user(12345)
    conv = await test_backend_client.create_conversation(title="Voice test")
    user_storage.set_user_mode(user.id, "talk", conversation_id=conv["id"])

    voice_mock = MagicMock(spec=Voice)
    voice_mock.file_id = "voice_file_abc"

    msg = _make_message(user=user, voice=voice_mock)
    msg.bot.get_file = AsyncMock(return_value=MagicMock(file_path="voice.ogg"))
    msg.bot.download_file = AsyncMock(return_value=io.BytesIO(b"fake-ogg-bytes"))

    cfg = TelegramConfig(
        bot_token="fake", allowed_user_id=12345, media_dir=tmp_path
    )

    transcriber = DeepgramTranscriber(
        mock_transcript="Голосовое сообщение расшифровано успешно"
    )

    processor = QueueProcessor(
        bot=msg.bot,
        backend_client=test_backend_client,
        storage=user_storage,
        transcriber=transcriber,
        turn_timeout_seconds=0.1,  # short timeout for test
    )

    await on_user_message(
        msg,
        storage=user_storage,
        client=test_backend_client,
        queue_processor=processor,
        config=cfg,
    )

    # Let background processing run
    await asyncio.sleep(0.3)

    # Verify voice file was written locally
    saved_file = tmp_path / "voice_voice_file_abc.ogg"
    assert saved_file.exists()
    assert saved_file.read_bytes() == b"fake-ogg-bytes"

    # Verify message was added to conversation
    messages = await test_backend_client.list_messages(conv["id"])
    assert len(messages) >= 1
    expected_text = (
        "[Голосовое сообщение (автоматическая расшифровка речи, возможны неточности распознавания)]:\n"
        "Голосовое сообщение расшифровано успешно"
    )
    assert messages[0]["text"] == expected_text
    assert messages[0]["metadata"]["media_type"] == "voice"


@sync_test
async def test_photo_message_processing(
    user_storage: UserStateStorage,
    test_backend_client: BackendClient,
    tmp_path: pathlib.Path,
) -> None:
    user = _make_user(12345)
    conv = await test_backend_client.create_conversation(title="Photo test")
    user_storage.set_user_mode(user.id, "talk", conversation_id=conv["id"])

    photo_size = MagicMock(spec=PhotoSize)
    photo_size.file_id = "photo_xyz"

    msg = _make_message(
        user=user, photo=[photo_size], caption="Вот скриншот ошибки"
    )
    msg.bot.get_file = AsyncMock(return_value=MagicMock(file_path="photo.jpg"))
    msg.bot.download_file = AsyncMock(return_value=io.BytesIO(b"fake-image-bytes"))

    cfg = TelegramConfig(
        bot_token="fake", allowed_user_id=12345, media_dir=tmp_path
    )

    processor = QueueProcessor(
        bot=msg.bot,
        backend_client=test_backend_client,
        storage=user_storage,
        transcriber=DeepgramTranscriber(mock_transcript="тест"),
        turn_timeout_seconds=0.1,
    )

    await on_user_message(
        msg,
        storage=user_storage,
        client=test_backend_client,
        queue_processor=processor,
        config=cfg,
    )

    await asyncio.sleep(0.3)

    saved_photo = tmp_path / "photo_photo_xyz.jpg"
    assert saved_photo.exists()
    assert saved_photo.read_bytes() == b"fake-image-bytes"

    messages = await test_backend_client.list_messages(conv["id"])
    assert len(messages) >= 1
    assert messages[0]["text"] == "Вот скриншот ошибки"
    assert messages[0]["metadata"]["media_type"] == "photo"


@sync_test
async def test_deepgram_transcriber_mock_and_parsing() -> None:
    # 1. No key -> fallback to mock
    t1 = DeepgramTranscriber(api_key=None)
    res1 = await t1.transcribe(b"dummy")
    assert "расшифровка" in res1

    # 2. Custom mock
    t2 = DeepgramTranscriber(api_key="key", mock_transcript="Пользовательский текст")
    res2 = await t2.transcribe(b"dummy")
    assert res2 == "Пользовательский текст"


def test_task_categories_keyboard_pagination() -> None:
    from kuzya.telegram.keyboards import task_categories_keyboard

    # Generate 15 dummy tasks with numbers
    dummy_tasks = [{"id": f"task-{i}", "title": f"Task {i}", "number": i} for i in range(1, 16)]

    # Page 1 (8 tasks, next page button exists)
    kb_p1 = task_categories_keyboard(active_category="incoming", tasks=dummy_tasks, page=1, page_size=8)
    all_buttons_p1 = [btn for row in kb_p1.inline_keyboard for btn in row]
    btn_texts_p1 = [b.text for b in all_buttons_p1]
    assert any(t == "📌 #1 Task 1" for t in btn_texts_p1)
    assert any(t == "📌 #8 Task 8" for t in btn_texts_p1)
    assert not any("Task 9" in t for t in btn_texts_p1)
    assert any("Стр. 1 из 2" in t for t in btn_texts_p1)
    assert any("➡️" in t for t in btn_texts_p1)

    # Page 2 (7 tasks, prev page button exists)
    kb_p2 = task_categories_keyboard(active_category="incoming", tasks=dummy_tasks, page=2, page_size=8)
    all_buttons_p2 = [btn for row in kb_p2.inline_keyboard for btn in row]
    btn_texts_p2 = [b.text for b in all_buttons_p2]
    assert not any(t == "📌 #1 Task 1" for t in btn_texts_p2)
    assert any(t == "📌 #9 Task 9" for t in btn_texts_p2)
    assert any(t == "📌 #15 Task 15" for t in btn_texts_p2)
    assert any("Стр. 2 из 2" in t for t in btn_texts_p2)
    assert any("⬅️" in t for t in btn_texts_p2)


@sync_test
async def test_waiting_for_input_task_user_reply_resumes(
    test_backend_client: BackendClient,
    user_storage: UserStateStorage,
    tmp_path: pathlib.Path,
) -> None:
    # 1. Create a task in waiting_for_input status
    task = await test_backend_client.create_task(
        title="Deploy skynet bot",
        description="Initial description",
        workspace="skynet_bot",
    )
    task_id = task["id"]
    await test_backend_client.patch_task(
        task_id,
        status="waiting_for_input",
        metadata={
            "runner_session_id": "session-xyz",
            "clarification": {
                "title": "Token ambiguity",
                "question": "Which bot token to use?",
            },
        },
    )

    # 2. Put user into task context
    user_storage.set_user_mode(12345, "task", task_id=task_id)

    # 3. Simulate user sending answer
    chat = Chat(id=999, type="private")
    user = _make_user(12345)
    msg = _make_message(
        text="Используй production токен из секретов",
        user=user,
        chat=chat,
        message_id=200,
    )

    cfg = TelegramConfig(bot_token="fake", allowed_user_id=12345, media_dir=tmp_path)
    processor = MagicMock()

    await on_user_message(
        msg,
        storage=user_storage,
        client=test_backend_client,
        queue_processor=processor,
        config=cfg,
    )

    # 4. Verify task updated to in_progress and answer recorded
    updated_task = await test_backend_client.get_task(task_id)
    assert updated_task["status"] == "in_progress"
    assert "Используй production токен из секретов" in updated_task["description"]
    assert msg.answer.called
    answer_text = msg.answer.call_args[0][0]
    assert "Ответ принят" in answer_text
    assert "возобновляет выполнение" in answer_text


@sync_test
async def test_setup_bot_commands() -> None:
    from kuzya.telegram.bot import BOT_COMMANDS, setup_bot_commands
    mock_bot = AsyncMock()
    await setup_bot_commands(mock_bot)
    assert mock_bot.set_my_commands.called
    args, kwargs = mock_bot.set_my_commands.call_args
    assert args[0] == BOT_COMMANDS
    commands_registered = [c.command for c in args[0]]
    assert "new" in commands_registered
    assert "talk" in commands_registered
    assert "list" in commands_registered
    assert "analyze" in commands_registered
    assert "reset" in commands_registered
    assert "start" in commands_registered


@sync_test
async def test_cmd_analyze(
    test_backend_client: BackendClient,
) -> None:
    from kuzya.telegram.handlers import cmd_analyze
    task = await test_backend_client.create_task(
        title="Check bot token",
        description="Verify token",
        workspace="skynet_bot",
    )
    await test_backend_client.patch_task(
        task["id"],
        status="done",
        metadata={"summary": "Token verified successfully."},
    )

    msg = _make_message(text=f"/analyze {task['id']}")
    await cmd_analyze(msg, client=test_backend_client)

    assert msg.answer.called
    text = msg.answer.call_args[0][0]
    # /analyze now submits a durable analysis operation and reports its real
    # status. With no worker running in this test it is pending, so the reply
    # must not fabricate analyzer findings or reuse the developer summary.
    assert "Анализ задачи" in text
    assert "запущен" in text
    assert "Сессия выполнена успешно" not in text
    assert "Token verified successfully." not in text


@sync_test
async def test_on_user_message_photo(
    test_backend_client: BackendClient,
    user_storage: UserStateStorage,
    tmp_path: pathlib.Path,
) -> None:
    user = _make_user(user_id=8888)
    user_storage.set_user_mode(user.id, "talk")
    conv = await test_backend_client.create_conversation(title="Talk photo test")
    user_storage.set_talk_conversation(user.id, conv["id"])

    processor = MagicMock(spec=QueueProcessor)
    processor.submit_message = AsyncMock()

    cfg = TelegramConfig(
        bot_token="fake:token",
        allowed_user_id=user.id,
        media_dir=tmp_path / "media",
    )

    photo_item = MagicMock(spec=PhotoSize)
    photo_item.file_id = "photo_abc123"

    msg = _make_message(
        user=user,
        photo=[photo_item],
        caption="Посмотри на скриншот",
    )
    fake_file = MagicMock()
    fake_file.file_path = "photos/file_0.jpg"
    msg.bot.get_file = AsyncMock(return_value=fake_file)
    msg.bot.download_file = AsyncMock(return_value=io.BytesIO(b"fake_jpeg_data"))

    await on_user_message(
        msg,
        storage=user_storage,
        client=test_backend_client,
        queue_processor=processor,
        config=cfg,
    )

    assert processor.submit_message.called
    kwargs = processor.submit_message.call_args[1]
    assert kwargs["media_type"] == "photo"
    assert kwargs["text"] == "Посмотри на скриншот"
    assert kwargs["media_path"] is not None
    assert pathlib.Path(kwargs["media_path"]).is_file()
    assert pathlib.Path(kwargs["media_path"]).read_bytes() == b"fake_jpeg_data"


@sync_test
async def test_on_user_message_document_text(
    test_backend_client: BackendClient,
    user_storage: UserStateStorage,
    tmp_path: pathlib.Path,
) -> None:
    from aiogram.types import Document
    user = _make_user(user_id=8889)
    user_storage.set_user_mode(user.id, "talk")
    conv = await test_backend_client.create_conversation(title="Talk doc test")
    user_storage.set_talk_conversation(user.id, conv["id"])

    processor = MagicMock(spec=QueueProcessor)
    processor.submit_message = AsyncMock()

    cfg = TelegramConfig(
        bot_token="fake:token",
        allowed_user_id=user.id,
        media_dir=tmp_path / "media",
    )

    doc = MagicMock(spec=Document)
    doc.file_id = "doc_xyz789"
    doc.file_name = "test_script.py"
    doc.file_size = 28

    msg = _make_message(
        user=user,
        document=doc,
        caption="Вот мой скрипт",
    )
    fake_file = MagicMock()
    fake_file.file_path = "documents/test_script.py"
    msg.bot.get_file = AsyncMock(return_value=fake_file)
    msg.bot.download_file = AsyncMock(return_value=io.BytesIO(b"print('hello from python!')\n"))

    await on_user_message(
        msg,
        storage=user_storage,
        client=test_backend_client,
        queue_processor=processor,
        config=cfg,
    )

    assert processor.submit_message.called
    kwargs = processor.submit_message.call_args[1]
    assert kwargs["media_type"] == "document"
    assert "Вот мой скрипт" in kwargs["text"]
    assert "print('hello from python!')" in kwargs["text"]
    assert "test_script.py" in kwargs["text"]
    assert kwargs["media_path"] is not None
    assert pathlib.Path(kwargs["media_path"]).is_file()


class _WorkspaceOnlyKnowledgeBase:
    """Knowledge base stub exposing only a temporary workspaces root."""

    def __init__(self, workspaces_root: pathlib.Path) -> None:
        self.workspaces_root = workspaces_root

    def resolve_file(self, query: str) -> None:
        return None


@sync_test
async def test_queue_processor_does_not_copy_media_to_shared_inbox(
    test_backend_client: BackendClient,
    user_storage: UserStateStorage,
    tmp_path: pathlib.Path,
) -> None:
    """The Telegram worker stores metadata only; the handler owns staging."""
    user = _make_user(12345)
    conv = await test_backend_client.create_conversation(title="Media owner test")

    workspaces_root = tmp_path / "workspaces"
    legacy_inbox = workspaces_root / "secretary" / "inbox"
    legacy_inbox.mkdir(parents=True)
    legacy = legacy_inbox / "photo_photo_xyz.jpg"
    legacy.write_bytes(b"fake-image-bytes")

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_path = media_dir / "photo_new.jpg"
    media_path.write_bytes(b"new-image-bytes")

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
    processor = QueueProcessor(
        bot=bot,
        backend_client=test_backend_client,
        storage=user_storage,
        transcriber=DeepgramTranscriber(mock_transcript="тест"),
        knowledge_base=_WorkspaceOnlyKnowledgeBase(workspaces_root),
        turn_timeout_seconds=0.1,
    )

    await processor.submit_message(
        user_id=user.id,
        chat_id=12345,
        telegram_message_id=5,
        context_mode="talk",
        context_task_id=None,
        context_conversation_id=conv["id"],
        text="смотри",
        media_type="photo",
        media_path=str(media_path),
    )
    await asyncio.sleep(0.4)

    messages = await test_backend_client.list_messages(conv["id"])
    user_messages = [m for m in messages if m["role"] == "user"]
    assert user_messages
    assert user_messages[0]["metadata"]["media_type"] == "photo"
    assert user_messages[0]["metadata"]["media_path"] == str(media_path)
    # The legacy shared inbox is untouched and the new file is not staged
    # there; the persistent conversation handler owns attachment staging.
    assert [item.name for item in legacy_inbox.iterdir()] == [
        "photo_photo_xyz.jpg"
    ]
    assert legacy.read_bytes() == b"fake-image-bytes"
    assert not (legacy_inbox / "photo_new.jpg").exists()





@pytest.mark.parametrize("recoverable", [True, False])
@sync_test
async def test_worker_preserves_talk_only_for_confirmed_recoverable_failure(
    user_storage: UserStateStorage, recoverable: bool,
) -> None:
    conversation_id = "connection-test"
    user_storage.set_talk_conversation(12345, conversation_id)
    user_storage.set_user_mode(12345, "talk", conversation_id=conversation_id)
    queued = user_storage.enqueue_message(
        user_id=12345, chat_id=12345, telegram_message_id=1,
        context_mode="talk", context_task_id=None,
        context_conversation_id=conversation_id, text="hello",
    )
    client = MagicMock()
    client.append_message = AsyncMock(return_value={"id": "input"})
    client.create_turn = AsyncMock(return_value={"id": "turn"})
    error = "PiReplyCommandError: model_connection_error;"
    if recoverable:
        error += " conversation_continuation_available;"
    client.get_turn = AsyncMock(return_value={"status": "failed", "error": error})
    bot = MagicMock()
    bot.send_message = AsyncMock()
    processor = QueueProcessor(
        bot=bot, backend_client=client, storage=user_storage,
        transcriber=DeepgramTranscriber(mock_transcript="test"),
        poll_interval_seconds=0.001, turn_timeout_seconds=1,
    )
    await processor.process_message(queued)
    state = user_storage.get_user_state(12345)
    assert state.active_conversation_id == (conversation_id if recoverable else None)
    text = bot.send_message.call_args.kwargs["text"]
    assert "Не удалось связаться" in text
    assert ("Контекст диалога сброшен" in text) is (not recoverable)
    assert "Время ожидания" not in text
