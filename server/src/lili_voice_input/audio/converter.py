from __future__ import annotations

import asyncio
import logging
import subprocess

from lili_voice_input.config import Settings

logger = logging.getLogger(__name__)
WAV_BYTES_PER_SECOND = 16_000 * 2
WAV_HEADER_ALLOWANCE = 4096


class AudioConversionError(RuntimeError):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class AudioConverter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def convert_to_wav(self, content: bytes) -> bytes:
        command = (
            self.settings.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            "pipe:0",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "pipe:1",
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                input=content,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.settings.ffmpeg_timeout_seconds,
            )
        except OSError as exc:
            raise AudioConversionError("dependency_unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioConversionError("conversion_timeout") from exc
        wav_content = result.stdout
        max_bytes = self.settings.stt_max_duration_seconds * WAV_BYTES_PER_SECOND + WAV_HEADER_ALLOWANCE
        if result.returncode != 0 or not wav_content:
            raise AudioConversionError("invalid_audio")
        if len(wav_content) > max_bytes:
            raise AudioConversionError("duration_exceeded")
        logger.info("AUDIO_CONVERSION event=success output_bytes=%s", len(wav_content))
        return wav_content

