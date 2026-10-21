"""Telegram Secretary adapter package."""

from .bot import create_bot
from .client import BackendClient
from .config import TelegramConfig
from .state import QueuedMessage, UserState, UserStateStorage
from .transcription import DeepgramTranscriber
from .worker import QueueProcessor

__all__ = [
    "BackendClient",
    "DeepgramTranscriber",
    "QueueProcessor",
    "QueuedMessage",
    "TelegramConfig",
    "UserState",
    "UserStateStorage",
    "create_bot",
]
