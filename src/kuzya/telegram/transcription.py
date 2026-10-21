"""Deepgram audio transcription client using httpx."""

from __future__ import annotations

import logging
from typing import Any
import httpx

logger = logging.getLogger(__name__)

DEEPGRAM_API_URL = "https://api.deepgram.com/v1/listen"


class TranscriptionError(Exception):
    """Raised when audio transcription fails."""


class DeepgramTranscriber:
    """Transcribes audio files using Deepgram REST API or mock in test mode."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        mock_transcript: str | None = None,
    ) -> None:
        self.api_key = api_key
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self.mock_transcript = mock_transcript

    async def close(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        content_type: str = "audio/ogg",
        language: str = "ru",
    ) -> str:
        """Transcribe raw audio bytes to text."""
        if self.mock_transcript is not None or not self.api_key:
            logger.info("Using mock transcription")
            return self.mock_transcript or "Это тестовая расшифровка голосового сообщения."

        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": content_type,
        }
        params = {
            "model": "nova-2",
            "smart_format": "true",
            "language": language,
        }

        try:
            response = await self._client.post(
                DEEPGRAM_API_URL,
                content=audio_bytes,
                headers=headers,
                params=params,
            )
        except Exception as error:
            raise TranscriptionError(f"Network error calling Deepgram: {error}") from error

        if not response.is_success:
            raise TranscriptionError(
                f"Deepgram returned HTTP {response.status_code}: {response.text}"
            )

        data = response.json()
        try:
            channels = data.get("results", {}).get("channels", [])
            if not channels:
                return ""
            alternatives = channels[0].get("alternatives", [])
            if not alternatives:
                return ""
            return str(alternatives[0].get("transcript", "")).strip()
        except Exception as error:
            raise TranscriptionError(
                f"Failed to parse Deepgram response: {error}"
            ) from error
