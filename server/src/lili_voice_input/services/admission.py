from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from lili_voice_input.services import metrics
from lili_voice_input.services.runtime import RedisRuntime, RuntimeUnavailable


class AdmissionRejected(RuntimeError):
    def __init__(self, reason: str, retry_after_ms: int = 5000) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_ms = retry_after_ms


@dataclass(slots=True)
class AdmissionLease:
    token: str
    subject: str
    transport: str
    wait_ms: int
    _release: Callable[[], Awaitable[None]]
    _released: bool = field(default=False, init=False)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._release()


@dataclass(slots=True)
class _Waiter:
    token: str
    subject: str
    transport: str
    started: float
    future: asyncio.Future[AdmissionLease]


class LocalAdmissionController:
    def __init__(self, maximum: int, queue_size: int, wait_seconds: float, subject_maximum: int) -> None:
        self.maximum = maximum
        self.queue_size = queue_size
        self.wait_seconds = wait_seconds
        self.subject_maximum = subject_maximum
        self.active = 0
        self._subjects: dict[str, int] = {}
        self._queue: deque[_Waiter] = deque()
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        subject: str,
        transport: str,
        on_queued: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> AdmissionLease:
        started = time.perf_counter()
        async with self._lock:
            if self._can_admit(subject) and not self._queue:
                return self._grant(uuid.uuid4().hex, subject, transport, started)
            if len(self._queue) >= self.queue_size:
                metrics.CAPACITY_REJECTIONS.labels("queue_full", transport).inc()
                raise AdmissionRejected("capacity_reached", round(self.wait_seconds * 1000))
            future: asyncio.Future[AdmissionLease] = asyncio.get_running_loop().create_future()
            waiter = _Waiter(uuid.uuid4().hex, subject, transport, started, future)
            self._queue.append(waiter)
            metrics.ADMISSION_QUEUE_DEPTH.set(len(self._queue))
            position = len(self._queue)
            self._drain()
            if future.done():
                return future.result()
        if on_queued is not None:
            await on_queued(position, min(round(position * 250), round(self.wait_seconds * 1000)))
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self.wait_seconds)
        except TimeoutError as exc:
            async with self._lock:
                self._queue = deque(item for item in self._queue if item.token != waiter.token)
                self._drain()
            metrics.CAPACITY_REJECTIONS.labels("queue_timeout", transport).inc()
            raise AdmissionRejected("queue_timeout", round(self.wait_seconds * 1000)) from exc
        except asyncio.CancelledError:
            async with self._lock:
                self._queue = deque(item for item in self._queue if item.token != waiter.token)
                self._drain()
            raise

    def _can_admit(self, subject: str) -> bool:
        return self.active < self.maximum and self._subjects.get(subject, 0) < self.subject_maximum

    def _grant(self, token: str, subject: str, transport: str, started: float) -> AdmissionLease:
        self.active += 1
        self._subjects[subject] = self._subjects.get(subject, 0) + 1
        wait_ms = round((time.perf_counter() - started) * 1000)
        metrics.ACTIVE_SESSIONS.labels(transport).inc()
        metrics.ADMISSION_WAIT.observe(wait_ms / 1000)

        async def release() -> None:
            async with self._lock:
                self.active = max(0, self.active - 1)
                count = self._subjects.get(subject, 0) - 1
                if count > 0:
                    self._subjects[subject] = count
                else:
                    self._subjects.pop(subject, None)
                metrics.ACTIVE_SESSIONS.labels(transport).dec()
                self._drain()

        return AdmissionLease(token, subject, transport, wait_ms, release)

    def _drain(self) -> None:
        while self.active < self.maximum and self._queue:
            eligible_index = next((i for i, item in enumerate(self._queue) if self._can_admit(item.subject)), None)
            if eligible_index is None:
                break
            waiter = self._queue[eligible_index]
            del self._queue[eligible_index]
            if not waiter.future.done():
                waiter.future.set_result(self._grant(waiter.token, waiter.subject, waiter.transport, waiter.started))
        metrics.ADMISSION_QUEUE_DEPTH.set(len(self._queue))


_ADMISSION_ACQUIRE_LUA = """
local active = KEYS[1]
local queue = KEYS[2]
local subjects = KEYS[3]
local token = ARGV[1]
local subject = ARGV[2]
local now = tonumber(ARGV[3])
local lease_expiry = tonumber(ARGV[4])
local queue_cutoff = tonumber(ARGV[5])
local max_active = tonumber(ARGV[6])
local max_queue = tonumber(ARGV[7])
local subject_max = tonumber(ARGV[8])
local subject_prefix = ARGV[9]

local stale = redis.call('ZRANGEBYSCORE', queue, '-inf', queue_cutoff)
for _, item in ipairs(stale) do redis.call('HDEL', subjects, item) end
redis.call('ZREMRANGEBYSCORE', queue, '-inf', queue_cutoff)
redis.call('ZREMRANGEBYSCORE', active, '-inf', now)

if not redis.call('ZSCORE', queue, token) then
  if redis.call('ZCARD', queue) >= max_queue then return {-1, -1} end
  redis.call('ZADD', queue, now, token)
  redis.call('HSET', subjects, token, subject)
end

local ordered = redis.call('ZRANGE', queue, 0, -1)
local first_eligible = nil
for _, item in ipairs(ordered) do
  local candidate_subject = redis.call('HGET', subjects, item)
  local candidate_key = subject_prefix .. candidate_subject
  redis.call('ZREMRANGEBYSCORE', candidate_key, '-inf', now)
  if redis.call('ZCARD', candidate_key) < subject_max then
    first_eligible = item
    break
  end
end

local rank = redis.call('ZRANK', queue, token) or 0
local subject_key = subject_prefix .. subject
redis.call('ZREMRANGEBYSCORE', subject_key, '-inf', now)
if first_eligible == token and redis.call('ZCARD', active) < max_active and redis.call('ZCARD', subject_key) < subject_max then
  redis.call('ZREM', queue, token)
  redis.call('HDEL', subjects, token)
  redis.call('ZADD', active, lease_expiry, token)
  redis.call('ZADD', subject_key, lease_expiry, token)
  redis.call('PEXPIRE', subject_key, lease_expiry - now + 1000)
  return {1, 0}
end
return {0, rank + 1}
"""

_ADMISSION_RELEASE_LUA = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
return 1
"""


class RedisAdmissionController:
    def __init__(
        self,
        runtime: RedisRuntime,
        prefix: str,
        maximum: int,
        queue_size: int,
        wait_seconds: float,
        subject_maximum: int,
        lease_ttl_seconds: int,
    ) -> None:
        self.runtime = runtime
        self.prefix = f"{prefix}:admission"
        self.maximum = maximum
        self.queue_size = queue_size
        self.wait_seconds = wait_seconds
        self.subject_maximum = subject_maximum
        self.lease_ttl_seconds = lease_ttl_seconds

    async def acquire(
        self,
        subject: str,
        transport: str,
        on_queued: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> AdmissionLease:
        token = uuid.uuid4().hex
        started = time.perf_counter()
        announced = False
        queue_counted = False
        subject_hash = uuid.uuid5(uuid.NAMESPACE_URL, subject).hex
        active_key = f"{self.prefix}:active"
        queue_key = f"{self.prefix}:queue"
        subjects_key = f"{self.prefix}:queue_subjects"
        subject_prefix = f"{self.prefix}:subject:"
        try:
            while True:
                now_ms = int(time.time() * 1000)
                result = await self.runtime.client.eval(
                    _ADMISSION_ACQUIRE_LUA,
                    3,
                    active_key,
                    queue_key,
                    subjects_key,
                    token,
                    subject_hash,
                    now_ms,
                    now_ms + self.lease_ttl_seconds * 1000,
                    now_ms - round(self.wait_seconds * 1000),
                    self.maximum,
                    self.queue_size,
                    self.subject_maximum,
                    subject_prefix,
                )
                status, position = int(result[0]), int(result[1])
                if status == 1:
                    if queue_counted:
                        metrics.REDIS_ADMISSION_QUEUE_DEPTH.dec()
                        queue_counted = False
                    wait_ms = round((time.perf_counter() - started) * 1000)

                    async def release() -> None:
                        try:
                            await self.runtime.client.eval(
                                _ADMISSION_RELEASE_LUA,
                                4,
                                active_key,
                                f"{subject_prefix}{subject_hash}",
                                queue_key,
                                subjects_key,
                                token,
                            )
                        except RuntimeUnavailable:
                            metrics.LEASE_RELEASE_FAILURES.labels("admission").inc()
                        except Exception as exc:  # noqa: BLE001 - Redis backend failure is handled as readiness loss.
                            metrics.LEASE_RELEASE_FAILURES.labels("admission").inc()
                            self.runtime.mark_unavailable(exc)

                    return AdmissionLease(token, subject, transport, wait_ms, release)
                if status == -1:
                    metrics.CAPACITY_REJECTIONS.labels("queue_full", transport).inc()
                    raise AdmissionRejected("capacity_reached", round(self.wait_seconds * 1000))
                if not queue_counted:
                    metrics.REDIS_ADMISSION_QUEUE_DEPTH.inc()
                    queue_counted = True
                if not announced and on_queued is not None:
                    announced = True
                    await on_queued(position, min(position * 250, round(self.wait_seconds * 1000)))
                if time.perf_counter() - started >= self.wait_seconds:
                    metrics.CAPACITY_REJECTIONS.labels("queue_timeout", transport).inc()
                    raise AdmissionRejected("queue_timeout", round(self.wait_seconds * 1000))
                await asyncio.sleep(0.1)
        except Exception as exc:
            if queue_counted:
                metrics.REDIS_ADMISSION_QUEUE_DEPTH.dec()
            if not isinstance(exc, (AdmissionRejected, RuntimeUnavailable)):
                self.runtime.mark_unavailable(exc)
            try:
                await self.runtime.client.eval(
                    _ADMISSION_RELEASE_LUA,
                    4,
                    active_key,
                    f"{subject_prefix}{subject_hash}",
                    queue_key,
                    subjects_key,
                    token,
                )
            except Exception:  # noqa: BLE001, S110 - best-effort removal after Redis failure
                pass
            if isinstance(exc, (AdmissionRejected, RuntimeUnavailable)):
                raise
            raise RuntimeUnavailable(self.runtime.error or "redis_unavailable") from exc
        except asyncio.CancelledError:
            if queue_counted:
                metrics.REDIS_ADMISSION_QUEUE_DEPTH.dec()
            try:
                await self.runtime.client.eval(
                    _ADMISSION_RELEASE_LUA,
                    4,
                    active_key,
                    f"{subject_prefix}{subject_hash}",
                    queue_key,
                    subjects_key,
                    token,
                )
            except Exception:  # noqa: BLE001, S110 - best-effort removal during cancellation
                pass
            raise


class HybridAdmissionController:
    def __init__(self, local: LocalAdmissionController, distributed: RedisAdmissionController) -> None:
        self.local = local
        self.distributed = distributed

    @property
    def active(self) -> int:
        return self.local.active

    async def acquire(
        self,
        subject: str,
        transport: str,
        on_queued: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> AdmissionLease:
        try:
            return await asyncio.wait_for(
                self._acquire_within_deadline(subject, transport, on_queued),
                timeout=self.distributed.wait_seconds,
            )
        except TimeoutError as exc:
            metrics.CAPACITY_REJECTIONS.labels("queue_timeout", transport).inc()
            raise AdmissionRejected("queue_timeout", round(self.distributed.wait_seconds * 1000)) from exc

    async def _acquire_within_deadline(
        self,
        subject: str,
        transport: str,
        on_queued: Callable[[int, int], Awaitable[None]] | None,
    ) -> AdmissionLease:
        distributed_lease = await self.distributed.acquire(subject, transport, on_queued)
        try:
            local_lease = await self.local.acquire(subject, transport, on_queued)
        except asyncio.CancelledError:
            await distributed_lease.release()
            raise
        except Exception:
            await distributed_lease.release()
            raise

        async def release() -> None:
            try:
                await distributed_lease.release()
            finally:
                await local_lease.release()

        return AdmissionLease(
            distributed_lease.token,
            subject,
            transport,
            local_lease.wait_ms + distributed_lease.wait_ms,
            release,
        )
