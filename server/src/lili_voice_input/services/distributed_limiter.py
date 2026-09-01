from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from lili_voice_input.services import metrics
from lili_voice_input.services.limiter import StageCapacityError
from lili_voice_input.services.runtime import RedisRuntime, RuntimeUnavailable

_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[5]))
return 1
"""


@dataclass(slots=True)
class DistributedLease:
    runtime: RedisRuntime | None
    key: str = ""
    token: str = ""
    _released: bool = field(default=False, init=False)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.runtime is not None:
            try:
                await self.runtime.client.zrem(self.key, self.token)
            except RuntimeUnavailable:
                metrics.LEASE_RELEASE_FAILURES.labels(_stage(self.key)).inc()
            except Exception as exc:  # noqa: BLE001 - Redis clients expose several backend-specific errors.
                metrics.LEASE_RELEASE_FAILURES.labels(_stage(self.key)).inc()
                self.runtime.mark_unavailable(exc)


class RedisLeaseLimiter:
    def __init__(
        self,
        runtime: RedisRuntime,
        key: str,
        maximum: int,
        lease_ttl_seconds: int,
        *,
        fail_open_on_runtime_loss: bool = True,
    ) -> None:
        self.runtime = runtime
        self.key = key
        self.maximum = maximum
        self.lease_ttl_seconds = lease_ttl_seconds
        self.fail_open_on_runtime_loss = fail_open_on_runtime_loss

    async def acquire(self, timeout_seconds: float) -> DistributedLease:
        if not self.runtime.enabled:
            return DistributedLease(None)
        token = uuid.uuid4().hex
        deadline = time.monotonic() + timeout_seconds
        while True:
            now_ms = int(time.time() * 1000)
            expiry_ms = now_ms + self.lease_ttl_seconds * 1000
            try:
                acquired = await self.runtime.client.eval(
                    _ACQUIRE_LUA,
                    1,
                    self.key,
                    now_ms,
                    self.maximum,
                    expiry_ms,
                    token,
                    self.lease_ttl_seconds * 1000 + 1000,
                )
            except RuntimeUnavailable:
                if self.fail_open_on_runtime_loss:
                    return DistributedLease(None)
                raise
            except Exception as exc:
                self.runtime.mark_unavailable(exc)
                if self.fail_open_on_runtime_loss:
                    return DistributedLease(None)
                raise RuntimeUnavailable(self.runtime.error or "redis_unavailable") from exc
            if int(acquired) == 1:
                return DistributedLease(self.runtime, self.key, token)
            if time.monotonic() >= deadline:
                raise StageCapacityError("distributed_capacity_timeout")
            await asyncio.sleep(0.05)


def _stage(key: str) -> str:
    if ":asr:" in key:
        return "asr"
    if ":polish:" in key:
        return "polish"
    return "unknown"
