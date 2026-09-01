from __future__ import annotations

import asyncio
import logging
import subprocess
import time

from lili_voice_input.config import Settings
from lili_voice_input.services import metrics
from lili_voice_input.services.limiter import BoundedLimiter, StageCapacityError

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
        self.limiter = BoundedLimiter(
            settings.ffmpeg_max_concurrency,
            settings.ffmpeg_queue_size,
            settings.ffmpeg_queue_timeout_seconds,
            inflight_metric=metrics.FFMPEG_INFLIGHT,
            queue_metric=metrics.FFMPEG_QUEUE_DEPTH,
            wait_metric=metrics.FFMPEG_QUEUE_WAIT,
        )

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
        started_at = time.perf_counter()
        try:
            async with self.limiter.slot() as slot:
                result = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    input=content,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=self.settings.ffmpeg_timeout_seconds,
                )
        except StageCapacityError as exc:
            metrics.FFMPEG_REQUESTS.labels("capacity_reached").inc()
            logger.warning("AUDIO_CONVERSION event=failure reason=capacity_reached input_bytes=%s", len(content))
            raise AudioConversionError("capacity_reached") from exc
        except OSError as exc:
            metrics.FFMPEG_REQUESTS.labels("dependency_unavailable").inc()
            logger.warning("AUDIO_CONVERSION event=failure reason=dependency_unavailable input_bytes=%s", len(content))
            raise AudioConversionError("dependency_unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            metrics.FFMPEG_REQUESTS.labels("conversion_timeout").inc()
            logger.warning("AUDIO_CONVERSION event=failure reason=conversion_timeout input_bytes=%s", len(content))
            raise AudioConversionError("conversion_timeout") from exc
        wav_content = result.stdout
        max_bytes = self.settings.stt_max_duration_seconds * WAV_BYTES_PER_SECOND + WAV_HEADER_ALLOWANCE
        if result.returncode != 0 or not wav_content:
            metrics.FFMPEG_REQUESTS.labels("invalid_audio").inc()
            logger.warning("AUDIO_CONVERSION event=failure reason=invalid_audio input_bytes=%s", len(content))
            raise AudioConversionError("invalid_audio")
        if len(wav_content) > max_bytes:
            metrics.FFMPEG_REQUESTS.labels("duration_exceeded").inc()
            raise AudioConversionError("duration_exceeded")
        metrics.FFMPEG_REQUESTS.labels("success").inc()
        metrics.FFMPEG_LATENCY.observe(time.perf_counter() - started_at)
        logger.info(
            "AUDIO_CONVERSION event=success input_bytes=%s output_bytes=%s queue_wait_ms=%s latency_ms=%s",
            len(content),
            len(wav_content),
            slot.wait_ms,
            round((time.perf_counter() - started_at) * 1000),
        )
        return wav_content
