from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from lili_voice_input.audio.merger import merge_transcript_segments
from lili_voice_input.audio.pcm import (
    FRAME_DURATION_MS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    milliseconds_to_bytes,
    pcm16_to_wav,
)
from lili_voice_input.audio.segmenter import AudioSegment, AudioSegmenter
from lili_voice_input.providers.interfaces import AsrProvider
from lili_voice_input.providers.openrouter_asr import AsrProviderError
from lili_voice_input.services import metrics
from lili_voice_input.services.asr_scheduler import AsrScheduler
from lili_voice_input.services.polishing import PolishingService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SegmentResult:
    sequence: int
    text: str | None
    latency_ms: int
    attempts: int
    error: AsrProviderError | None = None
    queue_wait_ms: int = 0


@dataclass(frozen=True, slots=True)
class FinalResult:
    text: str
    polished: bool
    polish_status: Literal["applied", "disabled", "fallback"]
    polish_reason: str | None
    degraded: bool
    degraded_stage: Literal["asr", "polish"] | None
    latency_ms: int
    polish_latency_ms: int
    total_latency_ms: int
    segment_count: int
    failed_segment_count: int
    admission_wait_ms: int = 0
    asr_queue_wait_ms: int = 0


class StreamingSession:
    def __init__(
        self,
        *,
        asr_provider: AsrProvider,
        polish_service: PolishingService,
        language: str | None = "zh",
        segment_target_seconds: int = 30,
        segment_max_seconds: int = 45,
        segment_overlap_ms: int = 1000,
        segment_silence_ms: int = 600,
        min_speech_ms: int = 200,
        segment_max_in_flight: int = 2,
        segment_max_retries: int = 2,
        request_timeout_seconds: float = 30,
        finalization_timeout_seconds: float = 120,
        max_duration_seconds: int = 600,
        session_wall_timeout_seconds: float = 660,
        asr_scheduler: AsrScheduler | None = None,
        admission_wait_ms: int = 0,
    ) -> None:
        self.session_id = uuid.uuid4().hex
        self.asr_provider = asr_provider
        self.polish_service = polish_service
        self.language = language
        self.segmenter = AudioSegmenter(
            target_seconds=segment_target_seconds,
            max_seconds=segment_max_seconds,
            overlap_ms=segment_overlap_ms,
            silence_ms=segment_silence_ms,
            min_speech_ms=min_speech_ms,
        )
        self.segment_max_retries = segment_max_retries
        self.request_timeout_seconds = request_timeout_seconds
        self.finalization_timeout_seconds = finalization_timeout_seconds
        self.max_duration_bytes = max_duration_seconds * SAMPLE_RATE * SAMPLE_WIDTH_BYTES
        self.session_wall_timeout_seconds = session_wall_timeout_seconds
        self.asr_scheduler = asr_scheduler
        self.admission_wait_ms = admission_wait_ms
        self._created_at = time.perf_counter()
        self._session_semaphore = asyncio.Semaphore(segment_max_in_flight)
        self._tasks: dict[int, asyncio.Task[SegmentResult]] = {}
        self._total_bytes = 0
        self._closed = False
        self._finalizing = False

    @property
    def segment_count(self) -> int:
        return len(self._tasks)

    async def add_audio(self, pcm: bytes) -> None:
        if self._closed or self._finalizing:
            raise ValueError("session_closed")
        if time.perf_counter() - self._created_at > self.session_wall_timeout_seconds:
            raise ValueError("session_wall_timeout")
        if self._total_bytes + len(pcm) > self.max_duration_bytes:
            raise ValueError("max_duration_exceeded")
        self._total_bytes += len(pcm)
        segment = self.segmenter.add_frame(pcm)
        if segment is not None:
            self._submit(segment)

    async def finalize(self) -> FinalResult:
        if self._closed or self._finalizing:
            raise ValueError("session_closed")
        if time.perf_counter() - self._created_at > self.session_wall_timeout_seconds:
            raise ValueError("session_wall_timeout")
        self._finalizing = True
        started_at = time.perf_counter()
        remainder = self.segmenter.finalize()
        if remainder is not None:
            self._submit(remainder)
        if not self.segmenter.has_speech or not self._tasks:
            await self.close()
            raise ValueError("empty_audio")
        _, pending = await asyncio.wait(list(self._tasks.values()), timeout=self.finalization_timeout_seconds)
        asr_latency_ms = round((time.perf_counter() - started_at) * 1000)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        results: list[SegmentResult] = []
        for sequence, task in sorted(self._tasks.items()):
            if task in pending or task.cancelled():
                results.append(SegmentResult(sequence, None, asr_latency_ms, 0, AsrProviderError("timeout"), 0))
                continue
            try:
                results.append(task.result())
            except Exception as exc:  # noqa: BLE001 - isolate failed segment tasks and preserve successful text
                logger.warning(
                    "SEGMENT event=task_failure session=%s sequence=%s error=%s",
                    self.session_id,
                    sequence,
                    type(exc).__name__,
                )
                results.append(SegmentResult(sequence, None, 0, 0, AsrProviderError("provider_error")))
        successful = [result.text for result in results if result.text]
        failed_count = len(results) - len(successful)
        if not successful:
            first_error = next((result.error for result in results if result.error), None)
            await self.close()
            raise first_error or AsrProviderError("provider_error")
        polish = await self.polish_service.polish(merge_transcript_segments(successful))
        total_latency_ms = round((time.perf_counter() - started_at) * 1000)
        if failed_count:
            degraded_stage = "asr"
        elif polish.status == "fallback":
            degraded_stage = "polish"
        else:
            degraded_stage = None
        result = FinalResult(
            text=polish.text,
            polished=polish.polished,
            polish_status=polish.status,
            polish_reason=polish.fallback_reason,
            degraded=failed_count > 0 or polish.status == "fallback",
            degraded_stage=degraded_stage,
            latency_ms=asr_latency_ms,
            polish_latency_ms=polish.latency_ms,
            total_latency_ms=total_latency_ms,
            segment_count=len(results),
            failed_segment_count=failed_count,
            admission_wait_ms=self.admission_wait_ms,
            asr_queue_wait_ms=sum(item.queue_wait_ms for item in results),
        )
        await self.close()
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.segmenter.close()
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self.asr_scheduler is not None:
            await self.asr_scheduler.cancel_session(self.session_id)

    def _submit(self, segment: AudioSegment) -> None:
        self._tasks[segment.sequence] = asyncio.create_task(self._transcribe_segment(segment))

    async def _transcribe_segment(self, segment: AudioSegment) -> SegmentResult:
        started_at = time.perf_counter()
        last_error: AsrProviderError | None = None
        attempts = 0
        queue_wait_ms = 0
        async with self._session_semaphore:
            for attempt in range(self.segment_max_retries + 1):
                attempts = attempt + 1
                try:
                    wav = pcm16_to_wav(segment.pcm)
                    if self.asr_scheduler is not None:
                        scheduled = await self.asr_scheduler.submit(
                            self.session_id,
                            wav,
                            audio_format="wav",
                            language=self.language,
                        )
                        text = scheduled.text
                        queue_wait_ms += scheduled.queue_wait_ms
                    else:
                        text = await asyncio.wait_for(
                            self.asr_provider.transcribe(wav, audio_format="wav", language=self.language),
                            timeout=self.request_timeout_seconds,
                        )
                    latency_ms = round((time.perf_counter() - started_at) * 1000)
                    logger.info(
                        "SEGMENT event=success session=%s sequence=%s duration_ms=%s attempts=%s latency_ms=%s",
                        self.session_id,
                        segment.sequence,
                        segment.duration_ms,
                        attempts,
                        latency_ms,
                    )
                    return SegmentResult(segment.sequence, text, latency_ms, attempts, queue_wait_ms=queue_wait_ms)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    last_error = AsrProviderError("timeout")
                except AsrProviderError as exc:
                    last_error = exc
                retryable = last_error.error_type in {"timeout", "rate_limited"} or (
                    last_error.error_type == "provider_error"
                    and (last_error.status_code is None or last_error.status_code >= 500)
                )
                if attempt >= self.segment_max_retries or not retryable:
                    break
                metrics.ASR_RETRIES.labels(last_error.error_type).inc()
                provider_delay = last_error.retry_after_seconds or 0
                delay = max(provider_delay, 0.5 * (2**attempt)) + random.uniform(0, 0.2)
                await asyncio.sleep(min(delay, 5.0))
        return SegmentResult(
            segment.sequence,
            None,
            round((time.perf_counter() - started_at) * 1000),
            attempts,
            last_error or AsrProviderError("provider_error"),
            queue_wait_ms,
        )


async def transcribe_pcm(
    pcm: bytes,
    *,
    asr_provider: AsrProvider,
    polish_service: PolishingService,
    stream_options: dict[str, object],
    language: str | None = "zh",
    asr_scheduler: AsrScheduler | None = None,
    admission_wait_ms: int = 0,
) -> FinalResult:
    session = StreamingSession(
        asr_provider=asr_provider,
        polish_service=polish_service,
        language=language,
        asr_scheduler=asr_scheduler,
        admission_wait_ms=admission_wait_ms,
        **stream_options,
    )
    frame_bytes = milliseconds_to_bytes(FRAME_DURATION_MS)
    try:
        for offset in range(0, len(pcm), frame_bytes):
            await session.add_audio(pcm[offset : offset + frame_bytes])
        return await session.finalize()
    except Exception:
        await session.close()
        raise
