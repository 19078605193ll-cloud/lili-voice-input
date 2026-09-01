from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

from lili_voice_input.providers.interfaces import TextPolisher
from lili_voice_input.providers.openai_polisher import PolishProviderError
from lili_voice_input.services import metrics
from lili_voice_input.services.distributed_limiter import RedisLeaseLimiter
from lili_voice_input.services.limiter import BoundedLimiter, StageCapacityError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PolishResult:
    text: str
    polished: bool
    status: Literal["applied", "disabled", "fallback"]
    latency_ms: int
    fallback_reason: str | None = None


class PolishingService:
    def __init__(
        self,
        provider: TextPolisher | None,
        *,
        enabled: bool,
        limiter: BoundedLimiter | None = None,
        global_limiter: RedisLeaseLimiter | None = None,
        queue_timeout_seconds: float = 3,
    ) -> None:
        self.provider = provider
        self.enabled = enabled
        self.limiter = limiter
        self.global_limiter = global_limiter
        self.queue_timeout_seconds = queue_timeout_seconds

    async def polish(self, transcript: str) -> PolishResult:
        original = transcript.strip()
        if not original:
            return PolishResult(original, False, "fallback", 0, "empty_input")
        if not self.enabled:
            return PolishResult(original, False, "disabled", 0)
        if self.provider is None:
            return PolishResult(original, False, "fallback", 0, "configuration_error")

        started_at = time.perf_counter()
        try:
            if self.limiter is None:
                output = await self.provider.polish(original)
            else:
                async with self.limiter.slot() as slot:
                    lease = None
                    try:
                        if self.global_limiter is not None:
                            remaining = self.queue_timeout_seconds - slot.wait_ms / 1000
                            if remaining <= 0:
                                raise StageCapacityError("queue_timeout")
                            lease = await self.global_limiter.acquire(remaining)
                        output = await self.provider.polish(original)
                    finally:
                        if lease is not None:
                            await lease.release()
            if not isinstance(output, str):
                raise ValueError("invalid_output")  # noqa: TRY004 - stable fallback reason
            text = output.strip()
            if not text:
                raise ValueError("empty_output")
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            metrics.POLISH_REQUESTS.labels("applied").inc()
            metrics.POLISH_LATENCY.observe(latency_ms / 1000)
            return PolishResult(text, True, "applied", latency_ms)
        except Exception as exc:  # noqa: BLE001 - polishing must never discard valid ASR text
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            reason = polish_failure_reason(exc)
            logger.warning(
                "POLISH event=fallback reason=%s exception_type=%s latency_ms=%s",
                reason,
                type(exc).__name__,
                latency_ms,
            )
            metrics.POLISH_REQUESTS.labels(reason).inc()
            return PolishResult(original, False, "fallback", latency_ms, reason)


def polish_failure_reason(exc: Exception) -> str:
    if isinstance(exc, StageCapacityError):
        return "capacity_reached"
    if isinstance(exc, PolishProviderError):
        return exc.reason
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, ValueError) and str(exc) in {"empty_output", "invalid_output"}:
        return str(exc)
    return "provider_error"
