"""Unified runtime orchestrator for Agent Manager.

Runs the FastAPI backend, background QueueDispatcher (secretary turns),
TaskExecutionWorker (developer background tasks), and Telegram Secretary bot.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from pathlib import Path
import shlex
import signal
import sys
from typing import Any, Sequence

from alembic import command
from alembic.config import Config
import uvicorn

from agent_manager.api import create_app
from agent_manager.dispatcher import QueueDispatcher
from agent_manager.knowledge import KnowledgeBase
from agent_manager.logging import configure_logging
from agent_manager.persistent_conversation import PersistentConversationHandler
from agent_manager.storage import Database
from agent_manager.task_worker import TaskExecutionWorker
from agent_manager.telegram.bot import create_bot, setup_bot_commands
from agent_manager.telegram.config import TelegramConfig
from agent_manager.telegram.notifier import TelegramNotifier

logger = logging.getLogger("agent_manager.runner")


@dataclasses.dataclass(frozen=True)
class RunnerConfig:
    """Configuration for the unified runner."""

    database_url: str
    host: str = "127.0.0.1"
    port: int = 18767
    artifact_root: Path = Path("~/.local/share/agent-manager/artifacts").expanduser()
    knowledge_root: Path = Path("~/.local/share/agent-manager/knowledge").expanduser()
    default_workspace: Path = Path.cwd()
    runner_argv_prefix: tuple[str, ...] = (sys.executable, "-m", "pi_runner")
    secretary_agent_id: str = "system-secretary"
    developer_agent_id: str = "agent-manager-developer"
    auto_migrate: bool = True
    telegram_config: TelegramConfig | None = None
    api_tokens: tuple[str, ...] = ()
    log_level: str = "INFO"
    json_logging: bool = False

    @classmethod
    def from_env(cls) -> RunnerConfig:
        db_url = os.environ.get(
            "AGENT_MANAGER_DB_URL",
            "sqlite:///" + str(Path("~/.local/share/agent-manager/experiments/2026-10-17/tasks.db").expanduser()),
        )
        host = os.environ.get("AGENT_MANAGER_HOST", "127.0.0.1")
        port = int(os.environ.get("AGENT_MANAGER_PORT", "18767"))

        artifact_root = Path(
            os.environ.get(
                "AGENT_MANAGER_ARTIFACT_ROOT",
                "~/.local/share/agent-manager/artifacts",
            )
        ).expanduser()

        knowledge_root = Path(
            os.environ.get(
                "AGENT_MANAGER_KNOWLEDGE_ROOT",
                "~/.local/share/agent-manager/knowledge",
            )
        ).expanduser()

        default_ws_str = os.environ.get("AGENT_MANAGER_DEFAULT_WORKSPACE")
        default_workspace = (
            Path(default_ws_str).expanduser().resolve()
            if default_ws_str
            else Path.cwd().resolve()
        )

        # Ensure Node 20+ and ~/.local/bin are present in PATH for Pi execution
        home = Path.home()
        extra_paths = [
            home / ".nvm/versions/node/v24.19.0/bin",
            home / ".local/bin",
            home / ".pi/agent/bin",
        ]
        current_path = os.environ.get("PATH", "")
        to_prepend = [
            str(p.resolve())
            for p in extra_paths
            if p.is_dir() and str(p.resolve()) not in current_path.split(":")
        ]
        if to_prepend:
            os.environ["PATH"] = ":".join(to_prepend) + ":" + current_path

        runner_cmd = os.environ.get("AGENT_MANAGER_RUNNER_PREFIX")
        if runner_cmd:
            runner_argv = tuple(shlex.split(runner_cmd))
        else:
            runner_argv = (sys.executable, "-m", "pi_runner")

        auto_migrate = os.environ.get("AGENT_MANAGER_AUTO_MIGRATE", "true").lower() in (
            "1",
            "true",
            "yes",
        )

        api_tokens_raw = os.environ.get("AGENT_MANAGER_API_TOKENS")
        if not api_tokens_raw:
            api_tokens_raw = os.environ.get("AGENT_MANAGER_API_TOKEN", "")
        api_tokens = tuple(
            t.strip() for t in api_tokens_raw.split(",") if t.strip()
        )

        log_level = os.environ.get("LOG_LEVEL", "INFO")
        json_logging = os.environ.get("LOG_FORMAT", "").lower() == "json"

        telegram_cfg: TelegramConfig | None = None
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            try:
                telegram_cfg = TelegramConfig.from_env()
            except Exception as e:
                logger.warning("Could not initialize TelegramConfig: %s", e)

        return cls(
            database_url=db_url,
            host=host,
            port=port,
            artifact_root=artifact_root,
            knowledge_root=knowledge_root,
            default_workspace=default_workspace,
            runner_argv_prefix=runner_argv,
            secretary_agent_id=os.environ.get(
                "AGENT_MANAGER_SECRETARY_AGENT_ID", "system-secretary"
            ),
            developer_agent_id=os.environ.get(
                "AGENT_MANAGER_DEVELOPER_AGENT_ID", "agent-manager-developer"
            ),
            auto_migrate=auto_migrate,
            telegram_config=telegram_cfg,
            api_tokens=api_tokens,
            log_level=log_level,
            json_logging=json_logging,
        )


def run_database_migrations(database_url: str) -> None:
    """Apply Alembic migrations up to head."""
    repo_root = Path(__file__).resolve().parents[2]
    ini_path = repo_root / "alembic.ini"
    migrations_dir = repo_root / "migrations"

    if not ini_path.exists() or not migrations_dir.exists():
        logger.info("Alembic files not found at %s, skipping auto-migration", ini_path)
        return

    logger.info("Applying Alembic migrations to %s", database_url)
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(migrations_dir))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    logger.info("Alembic migrations completed successfully.")


class AgentManagerServices:
    """Manages the full lifecycle of all background and server components."""

    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.database: Database | None = None
        self.queue_dispatcher: QueueDispatcher | None = None
        self.task_worker: TaskExecutionWorker | None = None
        self.bot = None
        self.dp = None
        self.telegram_notifier: TelegramNotifier | None = None
        self.uvicorn_server: uvicorn.Server | None = None
        self._stop_event = asyncio.Event()

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        """Initialize all subsystems and wire callbacks."""
        logger.info("Initializing AgentManagerServices...")

        # 1. Run migrations if enabled
        if self.config.auto_migrate:
            try:
                run_database_migrations(self.config.database_url)
            except Exception as e:
                logger.error("Failed to run database migrations: %s", e, exc_info=e)

        # 2. Storage
        self.database = Database(self.config.database_url)

        # 3. Knowledge Base
        self.knowledge_base = KnowledgeBase(self.config.knowledge_root)
        try:
            self.knowledge_base.initialize(database=self.database)
            logger.info("KnowledgeBase initialized at %s", self.config.knowledge_root)
        except Exception as e:
            logger.warning("Error initializing KnowledgeBase: %s", e)

        # 4. Telegram & Notifier
        if self.config.telegram_config and self.config.telegram_config.bot_token:
            try:
                bot, dp, _ = create_bot(
                    self.config.telegram_config,
                    knowledge_base=self.knowledge_base,
                )
                self.bot = bot
                self.dp = dp
                self.telegram_notifier = TelegramNotifier(
                    bot=self.bot,
                    loop=loop,
                    default_chat_id=self.config.telegram_config.allowed_user_id,
                )
                logger.info(
                    "Telegram Bot initialized for user %s",
                    self.config.telegram_config.allowed_user_id,
                )
            except Exception as e:
                logger.error("Failed to setup Telegram Bot: %s", e, exc_info=e)

        # 4. Secretary Turn Handler & Queue Dispatcher
        secretary_artifacts = self.config.artifact_root / "conversations"
        secretary_artifacts.mkdir(parents=True, exist_ok=True)
        secretary_handler = PersistentConversationHandler(
            self.database,
            agent_id=self.config.secretary_agent_id,
            runner_argv_prefix=list(self.config.runner_argv_prefix),
            artifact_root=secretary_artifacts,
            knowledge_base=self.knowledge_base,
        )
        self.queue_dispatcher = QueueDispatcher(
            self.database,
            handler=secretary_handler,
            poll_interval_seconds=1.0,
        )

        # 5. Task Execution Worker
        task_artifacts = self.config.artifact_root / "tasks"
        task_artifacts.mkdir(parents=True, exist_ok=True)

        on_task_finished = (
            self.telegram_notifier.notify_sync
            if self.telegram_notifier is not None
            else None
        )

        self.task_worker = TaskExecutionWorker(
            database=self.database,
            artifact_root=task_artifacts,
            agent_id=self.config.developer_agent_id,
            runner_argv_prefix=list(self.config.runner_argv_prefix),
            default_workspace=str(self.config.default_workspace),
            on_task_finished=on_task_finished,
            knowledge_base=self.knowledge_base,
            poll_interval_seconds=1.0,
        )

        # 6. FastAPI Uvicorn Server
        app = create_app(
            self.config.database_url,
            api_tokens=self.config.api_tokens,
        )
        uvicorn_cfg = uvicorn.Config(
            app=app,
            host=self.config.host,
            port=self.config.port,
            log_level=self.config.log_level.lower(),
            access_log=False,
        )
        self.uvicorn_server = uvicorn.Server(config=uvicorn_cfg)

    def start_background_threads(self) -> None:
        """Start QueueDispatcher and TaskExecutionWorker threads."""
        if self.queue_dispatcher:
            logger.info("Starting QueueDispatcher background thread...")
            self.queue_dispatcher.start()

        if self.task_worker:
            logger.info("Starting TaskExecutionWorker background thread...")
            self.task_worker.start()

    def stop_background_threads(self) -> None:
        """Stop background worker threads gracefully."""
        if self.task_worker:
            logger.info("Stopping TaskExecutionWorker...")
            try:
                self.task_worker.stop(timeout=5.0)
            except Exception as e:
                logger.warning("Error stopping TaskExecutionWorker: %s", e)

        if self.queue_dispatcher:
            logger.info("Stopping QueueDispatcher...")
            try:
                self.queue_dispatcher.stop(timeout=5.0)
            except Exception as e:
                logger.warning("Error stopping QueueDispatcher: %s", e)

        if self.database:
            try:
                self.database.dispose()
            except Exception:
                pass

    async def run(self) -> None:
        """Run async loops: uvicorn server and Telegram bot polling."""
        tasks: list[asyncio.Task[Any]] = []

        if self.uvicorn_server:
            tasks.append(asyncio.create_task(self.uvicorn_server.serve(), name="uvicorn"))

        if self.bot and self.dp:
            async def run_telegram_polling() -> None:
                try:
                    await setup_bot_commands(self.bot)
                    logger.info("Starting Telegram polling...")
                    await self.dp.start_polling(self.bot)
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.error("Telegram polling crashed: %s", exc, exc_info=exc)
                finally:
                    if self.bot.session:
                        await self.bot.session.close()

            tasks.append(asyncio.create_task(run_telegram_polling(), name="telegram_bot"))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            if self.uvicorn_server:
                self.uvicorn_server.should_exit = True
            if self.bot and self.dp:
                await self.dp.stop_polling()


async def main_async(config: RunnerConfig) -> None:
    configure_logging(
        level=config.log_level,
        json_format=config.json_logging,
    )
    logger.info("Agent Manager starting up...")

    loop = asyncio.get_running_loop()
    services = AgentManagerServices(config)
    services.setup(loop)
    services.start_background_threads()

    def on_signal() -> None:
        logger.info("Shutdown signal received, terminating services...")
        services.stop_background_threads()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except NotImplementedError:
            pass

    try:
        await services.run()
    finally:
        services.stop_background_threads()
        logger.info("Agent Manager shutdown complete.")


def main() -> None:
    config = RunnerConfig.from_env()
    try:
        asyncio.run(main_async(config))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
