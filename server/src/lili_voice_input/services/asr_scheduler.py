from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from lili_voice_input.providers.interfaces import AsrProvider
from lili_voice_input.providers.openrouter_asr import AsrProviderError
from lili_voice_input.services import metrics
from lili_voice_input.services.distributed_limiter import RedisLeaseLimiter
from lili_voice_input.services.limiter import StageCapacityError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduledTranscript:
    text: str
    queue_wait_ms: int


@dataclass(slots=True)
class _Job:
    token: str
    session_id: str
    audio: bytes
    audio_format: str
    language: str | None
    submitted_at: float
    started: asyncio.Event = field(default_factory=asyncio.Event)
    future: asyncio.Future[ScheduledTranscript] | None = None
    cancelled: bool = False
    provider_task: asyncio.Task[str] | None = None


class AsrScheduler:
    def __init__(
        self,
        provider: AsrProvider,
        *,
        concurrency: int,
        queue_size: int,
        queue_timeout_seconds: float,
        request_timeout_seconds: float,
        global_limiter: RedisLeaseLimiter,
        session_max_in_flight: int = 2,
    ) -> None:
        self.provider = provider
        self.concurrency = concurrency
        self.queue_size = queue_size
        self.queue_timeout_seconds = queue_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.global_limiter = global_limiter
        self.session_max_in_flight = session_max_in_flight
        self._queues: dict[str, deque[_Job]] = {}
        self._order: deque[str] = deque()
        self._queued = 0
        self._condition = asyncio.Condition()
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, _Job] = {}
        self._session_running: dict[str, int] = {}
        self._closed = False

    async def start(self) -> None:
        if not self._workers:
            self._workers = [
                asyncio.create_task(self._worker(), name=f"asr-worker-{i}") for i in range(self.concurrency)
            ]

    async def submit(
        self,
        session_id: str,
        audio: bytes,
        *,
        audio_format: str,
        language: str | None,
    ) -> ScheduledTranscript:
        if self._closed:
            raise AsrProviderError("provider_error")
        future: asyncio.Future[ScheduledTranscript] = asyncio.get_running_loop().create_future()
        job = _Job(uuid.uuid4().hex, session_id, audio, audio_format, language, time.perf_counter(), future=future)
        async with self._condition:
            if self._queued >= self.queue_size:
                metrics.ASR_REQUESTS.labels("queue_full").inc()
                logger.warning(
                    "ASR_SCHEDULER event=rejected reason=queue_full session=%s queue_depth=%s queue_limit=%s",
                    session_id,
                    self._queued,
                    self.queue_size,
                )
                raise AsrProviderError("queue_timeout")
            queue = self._queues.setdefault(session_id, deque())
            if not queue:
                self._order.append(session_id)
            queue.append(job)
            self._queued += 1
            metrics.ASR_QUEUE_DEPTH.set(self._queued)
            self._condition.notify()
        try:
            await asyncio.wait_for(job.started.wait(), timeout=self.queue_timeout_seconds)
            return await future
        except TimeoutError as exc:
            job.cancelled = True
            await self._remove_queued_job(job)
            if not future.done():
                future.cancel()
            metrics.ASR_REQUESTS.labels("queue_timeout").inc()
            logger.warning(
                "ASR_SCHEDULER event=failure reason=queue_timeout session=%s waited_ms=%s queue_depth=%s",
                session_id,
                round((time.perf_counter() - job.submitted_at) * 1000),
                self._queued,
            )
            raise AsrProviderError("queue_timeout") from exc
        except asyncio.CancelledError:
            job.cancelled = True
            await self._remove_queued_job(job)
            if job.provider_task is not None:
                job.provider_task.cancel()
            if not future.done():
                future.cancel()
            raise

    async def cancel_session(self, session_id: str) -> None:
        async with self._condition:
            queued = self._queues.pop(session_id, deque())
            self._order = deque(item for item in self._order if item != session_id)
            for job in queued:
                job.cancelled = True
                if job.future is not None and not job.future.done():
                    job.future.cancel()
            self._queued = max(0, self._queued - len(queued))
            metrics.ASR_QUEUE_DEPTH.set(self._queued)
        for job in list(self._running.values()):
            if job.session_id == session_id:
                job.cancelled = True
                if job.provider_task is not None:
                    job.provider_task.cancel()

    async def _remove_queued_job(self, target: _Job) -> None:
        async with self._condition:
            queue = self._queues.get(target.session_id)
            if queue is None or not any(job.token == target.token for job in queue):
                return
            self._queues[target.session_id] = deque(job for job in queue if job.token != target.token)
            self._queued = max(0, self._queued - 1)
            if not self._queues[target.session_id]:
                self._queues.pop(target.session_id, None)
                self._order = deque(item for item in self._order if item != target.session_id)
            metrics.ASR_QUEUE_DEPTH.set(self._queued)
            self._condition.notify_all()

    async def close(self) -> None:
        self._closed = True
        async with self._condition:
            for queue in self._queues.values():
                for job in queue:
                    job.cancelled = True
                    if job.future is not None and not job.future.done():
                        job.future.cancel()
            self._queues.clear()
            self._order.clear()
            self._queued = 0
            metrics.ASR_QUEUE_DEPTH.set(0)
            self._condition.notify_all()
        for job in self._running.values():
            job.cancelled = True
            if job.provider_task is not None:
                job.provider_task.cancel()
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _next_job(self) -> _Job | None:
        async with self._condition:
            while not self._closed:
                for _ in range(len(self._order)):
                    session_id = self._order.popleft()
                    if self._session_running.get(session_id, 0) >= self.session_max_in_flight:
                        self._order.append(session_id)
                        continue
                    queue = self._queues[session_id]
                    job = queue.popleft()
                    self._queued = max(0, self._queued - 1)
                    if queue:
                        self._order.append(session_id)
                    else:
                        self._queues.pop(session_id, None)
                    self._session_running[session_id] = self._session_running.get(session_id, 0) + 1
                    metrics.ASR_QUEUE_DEPTH.set(self._queued)
                    return job
                await self._condition.wait()
            return None

    async def _worker(self) -> None:
        while not self._closed:
            job = await self._next_job()
            if job is None:
                return
            lease = None
            provider_started_at: float | None = None
            try:
                if job.cancelled or (job.future is not None and job.future.cancelled()):
                    continue
                elapsed = time.perf_counter() - job.submitted_at
                remaining = self.queue_timeout_seconds - elapsed
                if remaining <= 0:
                    if job.future is not None and not job.future.done():
                        job.future.set_exception(AsrProviderError("queue_timeout"))
                    continue
                lease = await self.global_limiter.acquire(remaining)
                if job.cancelled:
                    continue
                queue_wait_ms = round((time.perf_counter() - job.submitted_at) * 1000)
                metrics.ASR_QUEUE_WAIT.observe(queue_wait_ms / 1000)
                job.started.set()
                self._running[job.token] = job
                metrics.ASR_INFLIGHT.inc()
                provider_started_at = time.perf_counter()
                job.provider_task = asyncio.create_task(
                    self.provider.transcribe(job.audio, audio_format=job.audio_format, language=job.language)
                )
                text = await asyncio.wait_for(job.provider_task, timeout=self.request_timeout_seconds)
                metrics.ASR_LATENCY.observe(time.perf_counter() - provider_started_at)
                metrics.ASR_REQUESTS.labels("success").inc()
                if job.future is not None and not job.future.done():
                    job.future.set_result(ScheduledTranscript(text, queue_wait_ms))
            except (TimeoutError, StageCapacityError) as exc:
                error = AsrProviderError("queue_timeout") if not job.started.is_set() else AsrProviderError("timeout")
                metrics.ASR_REQUESTS.labels(error.error_type).inc()
                logger.warning(
                    "ASR_SCHEDULER event=failure reason=%s session=%s exception_type=%s queue_wait_ms=%s "
                    "provider_elapsed_ms=%s request_timeout_seconds=%s",
                    error.error_type,
                    job.session_id,
                    type(exc).__name__,
                    round((provider_started_at - job.submitted_at) * 1000) if provider_started_at is not None else None,
                    round((time.perf_counter() - provider_started_at) * 1000)
                    if provider_started_at is not None
                    else None,
                    self.request_timeout_seconds,
                )
                if job.future is not None and not job.future.done():
                    job.future.set_exception(error)
            except asyncio.CancelledError:
                if self._closed:
                    return
                if job.future is not None and not job.future.done():
                    job.future.cancel()
            except AsrProviderError as exc:
                metrics.ASR_REQUESTS.labels(exc.error_type).inc()
                if job.future is not None and not job.future.done():
                    job.future.set_exception(exc)
            except Exception:  # noqa: BLE001 - isolate arbitrary provider adapter failures
                metrics.ASR_REQUESTS.labels("provider_error").inc()
                if job.future is not None and not job.future.done():
                    job.future.set_exception(AsrProviderError("provider_error"))
            finally:
                self._running.pop(job.token, None)
                job.provider_task = None
                async with self._condition:
                    running = self._session_running.get(job.session_id, 0) - 1
                    if running > 0:
                        self._session_running[job.session_id] = running
                    else:
                        self._session_running.pop(job.session_id, None)
                    self._condition.notify_all()
                if job.started.is_set():
                    metrics.ASR_INFLIGHT.dec()
                if lease is not None:
                    try:
                        await lease.release()
                    except Exception:  # noqa: BLE001, S110 - release is best effort after runtime loss
                        pass
