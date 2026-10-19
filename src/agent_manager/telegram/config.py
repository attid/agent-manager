"""Configuration for the Telegram Secretary adapter."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class TelegramConfig:
    """Runtime configuration for the Telegram bot interface."""

    bot_token: str
    allowed_user_id: int
    backend_url: str = "http://127.0.0.1:8000"
    api_token: str | None = None
    deepgram_api_key: str | None = None
    state_db_path: Path = dataclasses.field(
        default_factory=lambda: Path.home()
        / ".local/share/agent-manager/telegram_state.db"
    )
    media_dir: Path = dataclasses.field(
        default_factory=lambda: Path.home()
        / ".local/share/agent-manager/telegram_media"
    )
    default_workspace: Path | None = None

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        """Load configuration from environment variables."""
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "mock-token")
        allowed_id_raw = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0")
        try:
            allowed_user_id = int(allowed_id_raw)
        except ValueError:
            allowed_user_id = 0

        backend_url = os.environ.get(
            "AGENT_MANAGER_BACKEND_URL", "http://127.0.0.1:8000"
        ).rstrip("/")

        api_token = os.environ.get("AGENT_MANAGER_API_TOKEN")
        if not api_token and os.environ.get("AGENT_MANAGER_API_TOKENS"):
            tokens = [
                t.strip()
                for t in os.environ["AGENT_MANAGER_API_TOKENS"].split(",")
                if t.strip()
            ]
            if tokens:
                api_token = tokens[0]

        deepgram_api_key = os.environ.get("DEEPGRAM_API_KEY")

        state_db_raw = os.environ.get("TELEGRAM_STATE_DB_PATH")
        state_db_path = (
            Path(state_db_raw).expanduser().resolve()
            if state_db_raw
            else Path.home() / ".local/share/agent-manager/telegram_state.db"
        )

        media_dir_raw = os.environ.get("TELEGRAM_MEDIA_DIR")
        media_dir = (
            Path(media_dir_raw).expanduser().resolve()
            if media_dir_raw
            else Path.home() / ".local/share/agent-manager/telegram_media"
        )

        def_ws_raw = os.environ.get("AGENT_MANAGER_DEFAULT_WORKSPACE")
        default_workspace = (
            Path(def_ws_raw).expanduser().resolve()
            if def_ws_raw
            else None
        )

        return cls(
            bot_token=bot_token,
            allowed_user_id=allowed_user_id,
            backend_url=backend_url,
            api_token=api_token,
            deepgram_api_key=deepgram_api_key,
            state_db_path=state_db_path,
            media_dir=media_dir,
            default_workspace=default_workspace,
        )
