from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Literal

from lili_voice_input.providers.interfaces import TextPolisher
from lili_voice_input.providers.openai_polisher import PolishProviderError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PolishResult:
    text: str
    polished: bool
    status: Literal["applied", "disabled", "fallback"]
    latency_ms: int
    fallback_reason: str | None = None


class PolishingService:
    def __init__(self, provider: TextPolisher | None, *, enabled: bool) -> None:
        self.provider = provider
        self.enabled = enabled

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
            output = await self.provider.polish(original)
            if not isinstance(output, str):
                raise ValueError("invalid_output")
            text = output.strip()
            if not text:
                raise ValueError("empty_output")
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            return PolishResult(text, True, "applied", latency_ms)
        except Exception as exc:  # a polish failure must never discard a valid ASR transcript
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            reason = polish_failure_reason(exc)
            logger.warning(
                "POLISH event=fallback reason=%s exception_type=%s latency_ms=%s",
                reason,
                type(exc).__name__,
                latency_ms,
            )
            return PolishResult(original, False, "fallback", latency_ms, reason)


def polish_failure_reason(exc: Exception) -> str:
    if isinstance(exc, PolishProviderError):
        return exc.reason
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, ValueError) and str(exc) in {"empty_output", "invalid_output"}:
        return str(exc)
    return "provider_error"
