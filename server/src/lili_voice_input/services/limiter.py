from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from prometheus_client import Gauge, Histogram


class StageCapacityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Slot:
    wait_ms: int


class BoundedLimiter:
    def __init__(
        self,
        concurrency: int,
        queue_size: int,
        queue_timeout_seconds: float,
        *,
        inflight_metric: Gauge | None = None,
        queue_metric: Gauge | None = None,
        wait_metric: Histogram | None = None,
    ) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._queue_size = queue_size
        self._timeout = queue_timeout_seconds
        self._waiting = 0
        self._lock = asyncio.Lock()
        self._inflight_metric = inflight_metric
        self._queue_metric = queue_metric
        self._wait_metric = wait_metric

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[Slot]:
        started = time.perf_counter()
        async with self._lock:
            if self._semaphore.locked() and self._waiting >= self._queue_size:
                raise StageCapacityError("capacity_reached")
            self._waiting += 1
            if self._queue_metric is not None:
                self._queue_metric.set(self._waiting)
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=self._timeout)
            except TimeoutError as exc:
                raise StageCapacityError("queue_timeout") from exc
        finally:
            async with self._lock:
                self._waiting = max(0, self._waiting - 1)
                if self._queue_metric is not None:
                    self._queue_metric.set(self._waiting)
        wait_seconds = time.perf_counter() - started
        if self._wait_metric is not None:
            self._wait_metric.observe(wait_seconds)
        if self._inflight_metric is not None:
            self._inflight_metric.inc()
        try:
            yield Slot(round(wait_seconds * 1000))
        finally:
            if self._inflight_metric is not None:
                self._inflight_metric.dec()
            self._semaphore.release()
