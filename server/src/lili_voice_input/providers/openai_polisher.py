from __future__ import annotations

import logging
import time
from pathlib import Path

from openai import AsyncOpenAI

from lili_voice_input.config import Settings
from lili_voice_input.services import metrics

logger = logging.getLogger(__name__)


class PolishProviderError(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        status_code: int | None = None,
        provider_code: str | None = None,
        request_id: str | None = None,
        exception_type: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.provider_code = provider_code
        self.request_id = request_id
        self.exception_type = exception_type


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
        prompts_path = Path(__file__).resolve().parent.parent / "prompts"
        self.system_prompt = (prompts_path / "stt_polish_system_prompt.txt").read_text(encoding="utf-8").strip()
        self.user_prompt_template = (prompts_path / "stt_polish_user_prompt.txt").read_text(encoding="utf-8").strip()

    async def polish(self, transcript: str) -> str:
        if not self.settings.polish_api_key.strip() or not self.settings.polish_model.strip():
            raise PolishProviderError("configuration_error")
        started_at = time.perf_counter()
        try:
            user_prompt = self.user_prompt_template.format(user_message=transcript)
            request: dict[str, object] = {
                "model": self.settings.polish_model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_prompt},
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
                choice = completion.choices[0]
                content = choice.message.content
            except (AttributeError, IndexError, TypeError) as exc:
                raise PolishProviderError("invalid_output", provider_code="missing_choice") from exc
            if getattr(choice, "finish_reason", None) == "length":
                raise PolishProviderError("invalid_output", provider_code="finish_reason_length")
            if not isinstance(content, str):
                raise PolishProviderError("invalid_output")
            text = content.strip()
            if not text:
                raise PolishProviderError("empty_output")
            usage = getattr(completion, "usage", None)
            for kind, value in (
                ("prompt", getattr(usage, "prompt_tokens", None)),
                ("completion", getattr(usage, "completion_tokens", None)),
                ("total", getattr(usage, "total_tokens", None)),
            ):
                if isinstance(value, int) and value >= 0:
                    metrics.POLISH_TOKENS.labels(kind).inc(value)
            logger.info(
                "POLISH_PROVIDER event=success latency_ms=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
                round((time.perf_counter() - started_at) * 1000),
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
                getattr(usage, "total_tokens", None),
            )
            return text
        except Exception as exc:  # provider SDKs expose several transport exception types
            if isinstance(exc, PolishProviderError):
                error = exc
            else:
                error = PolishProviderError(
                    normalize_provider_error(exc),
                    status_code=getattr(exc, "status_code", None),
                    provider_code=extract_provider_code(exc),
                    request_id=extract_request_id(exc),
                    exception_type=type(exc).__name__,
                )
            logger.warning(
                "POLISH_PROVIDER event=failure reason=%s status_code=%s provider_code=%s "
                "exception_type=%s request_id=%s latency_ms=%s",
                error.reason,
                error.status_code,
                error.provider_code,
                error.exception_type or type(exc).__name__,
                error.request_id,
                round((time.perf_counter() - started_at) * 1000),
            )
            if error is exc:
                raise
            raise error from exc

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


def extract_provider_code(exc: Exception) -> str | None:
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else body.get("code") if isinstance(body, dict) else None
    return str(code)[:80] if isinstance(code, (str, int)) else None


def extract_request_id(exc: Exception) -> str | None:
    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id[:128]
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        for name in ("x-request-id", "request-id", "cf-ray"):
            value = headers.get(name)
            if value:
                return str(value)[:128]
    return None
