from __future__ import annotations

import logging

from redis.asyncio import Redis

from lili_voice_input.config import Settings
from lili_voice_input.services import metrics

logger = logging.getLogger(__name__)

_RATE_LIMIT_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


class RuntimeUnavailable(RuntimeError):
    pass


class RedisRuntime:
    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.redis_enabled
        self._client: Redis | None = (
            Redis.from_url(settings.redis_url, decode_responses=True) if settings.redis_enabled else None
        )
        self.error: str | None = None
        metrics.REDIS_READY.set(1 if not settings.redis_enabled else 0)

    @property
    def client(self) -> Redis:
        if self._client is None or self.error is not None:
            raise RuntimeUnavailable(self.error or "redis_disabled")
        return self._client

    @property
    def ready(self) -> bool:
        return not self.enabled or (self._client is not None and self.error is None)

    def mark_unavailable(self, exc: BaseException) -> None:
        self.error = f"Redis unavailable: {type(exc).__name__}"
        metrics.REDIS_READY.set(0)
        logger.error("REDIS event=unavailable error=%s", type(exc).__name__)

    async def start(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.ping()
            self.error = None
            metrics.REDIS_READY.set(1)
        except Exception as exc:  # noqa: BLE001 - Redis clients expose several backend-specific errors.
            self.mark_unavailable(exc)

    async def check(self) -> bool:
        if self._client is None:
            return True
        try:
            await self._client.ping()
            self.error = None
            metrics.REDIS_READY.set(1)
            return True
        except Exception as exc:  # noqa: BLE001 - health checks must convert any Redis backend failure.
            self.mark_unavailable(exc)
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class FixedWindowRateLimiter:
    def __init__(self, runtime: RedisRuntime, prefix: str) -> None:
        self.runtime = runtime
        self.prefix = prefix
        self._local: dict[str, tuple[int, int]] = {}
        import asyncio

        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, *, window_seconds: int = 60) -> bool:
        import time

        window = int(time.time()) // window_seconds
        redis_key = f"{self.prefix}:rate:{key}:{window}"
        if self.runtime.enabled:
            try:
                client = self.runtime.client
                count = await client.eval(_RATE_LIMIT_LUA, 1, redis_key, window_seconds + 2)
                return int(count) <= limit
            except RuntimeUnavailable:
                raise
            except Exception as exc:
                self.runtime.mark_unavailable(exc)
                raise RuntimeUnavailable(self.runtime.error or "redis_unavailable") from exc
        async with self._lock:
            current_window, count = self._local.get(key, (window, 0))
            if current_window != window:
                current_window, count = window, 0
            count += 1
            self._local[key] = (current_window, count)
            if len(self._local) > 10_000:
                self._local = {k: v for k, v in self._local.items() if v[0] >= window - 1}
            return count <= limit
