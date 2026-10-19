"""Bot and Dispatcher factory for Telegram Secretary adapter."""

from __future__ import annotations

import logging
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeDefault

from agent_manager.knowledge import KnowledgeBase
from .client import BackendClient
from .config import TelegramConfig
from .handlers import AuthMiddleware, router
from .state import UserStateStorage
from .transcription import DeepgramTranscriber
from .worker import QueueProcessor

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="new", description="Поставить новую задачу"),
    BotCommand(command="talk", description="Общий разговор со секретарём"),
    BotCommand(command="list", description="Список задач и карточек"),
    BotCommand(command="analyze", description="Анализ сессий и уроков задач"),
    BotCommand(command="reset", description="Сбросить диалог со секретарём"),
    BotCommand(command="start", description="Главное меню и статус"),
]


async def setup_bot_commands(bot: Bot) -> None:
    """Register native Telegram menu commands."""
    try:
        await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())
        logger.info("Registered Telegram bot commands: %s", [c.command for c in BOT_COMMANDS])
    except Exception as exc:
        logger.warning("Failed to register Telegram bot commands: %s", exc)


def create_bot(
    config: TelegramConfig,
    knowledge_base: KnowledgeBase | None = None,
) -> tuple[Bot, Dispatcher, QueueProcessor]:
    """Create and configure aiogram Bot, Dispatcher, and QueueProcessor."""
    bot = Bot(token=config.bot_token)
    dp = Dispatcher()

    backend_client = BackendClient(
        base_url=config.backend_url,
        api_token=config.api_token,
    )
    storage = UserStateStorage(config.state_db_path)
    transcriber = DeepgramTranscriber(api_key=config.deepgram_api_key)
    kb = knowledge_base or KnowledgeBase()

    queue_processor = QueueProcessor(
        bot=bot,
        backend_client=backend_client,
        storage=storage,
        transcriber=transcriber,
        knowledge_base=kb,
    )

    # Middleware for user authorization
    dp.message.middleware(AuthMiddleware(config.allowed_user_id))
    dp.callback_query.middleware(AuthMiddleware(config.allowed_user_id))

    # Dependency injection into handler workflow data
    dp["config"] = config
    dp["client"] = backend_client
    dp["storage"] = storage
    dp["transcriber"] = transcriber
    dp["queue_processor"] = queue_processor
    dp["knowledge_base"] = kb

    dp.include_router(router)

    return bot, dp, queue_processor


async def run_bot(config: TelegramConfig | None = None) -> None:
    """Run the Telegram Secretary bot via long-polling."""
    cfg = config or TelegramConfig.from_env()
    bot, dp, _ = create_bot(cfg)
    logger.info("Starting Telegram bot for user ID: %s", cfg.allowed_user_id)
    try:
        await setup_bot_commands(bot)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

