from __future__ import annotations

import logging
from pathlib import Path
import time

from openai import AsyncOpenAI

from lili_voice_input.config import Settings

logger = logging.getLogger(__name__)


class PolishProviderError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OpenAICompatiblePolisher:
    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        self.settings = settings
        self._client = client or AsyncOpenAI(
            api_key=settings.polish_api_key or "not-configured",
            base_url=settings.polish_base_url.rstrip("/"),
            timeout=settings.polish_timeout_seconds,
            max_retries=settings.polish_max_retries,
        )
        self._owns_client = client is None
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "stt_polish_system_prompt.txt"
        self.system_prompt = prompt_path.read_text(encoding="utf-8").strip()

    async def polish(self, transcript: str) -> str:
        if not self.settings.polish_api_key.strip() or not self.settings.polish_model.strip():
            raise PolishProviderError("configuration_error")
        started_at = time.perf_counter()
        try:
            request: dict[str, object] = {
                "model": self.settings.polish_model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": transcript},
                ],
                "temperature": self.settings.polish_temperature,
                "max_tokens": self.settings.polish_max_tokens,
            }
            if "deepseek" in self.settings.polish_model.casefold():
                # DMX DeepSeek requests must explicitly disable thinking instead of relying on a provider default.
                request["extra_body"] = {"thinking": {"type": "disabled"}}
            elif self.settings.polish_enable_thinking is not None:
                request["extra_body"] = {"enable_thinking": self.settings.polish_enable_thinking}
            completion = await self._client.chat.completions.create(**request)
            try:
                content = completion.choices[0].message.content
            except (AttributeError, IndexError, TypeError) as exc:
                raise PolishProviderError("invalid_output") from exc
            if not isinstance(content, str):
                raise PolishProviderError("invalid_output")
            text = content.strip()
            if not text:
                raise PolishProviderError("empty_output")
            usage = getattr(completion, "usage", None)
            logger.info(
                "POLISH_PROVIDER event=success latency_ms=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
                round((time.perf_counter() - started_at) * 1000),
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
                getattr(usage, "total_tokens", None),
            )
            return text
        except PolishProviderError:
            raise
        except Exception as exc:  # provider SDKs expose several transport exception types
            reason = normalize_provider_error(exc)
            logger.warning(
                "POLISH_PROVIDER event=failure reason=%s exception_type=%s latency_ms=%s",
                reason,
                type(exc).__name__,
                round((time.perf_counter() - started_at) * 1000),
            )
            raise PolishProviderError(reason) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()


def normalize_provider_error(exc: Exception) -> str:
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    if name in {"APITimeoutError", "TimeoutError"} or status_code in {408, 504}:
        return "timeout"
    if name == "RateLimitError" or status_code == 429:
        return "rate_limited"
    if name in {"APIConnectionError", "ConnectError", "NetworkError"}:
        return "network_error"
    if status_code in {401, 402, 403, 404}:
        return "configuration_error"
    return "provider_error"
