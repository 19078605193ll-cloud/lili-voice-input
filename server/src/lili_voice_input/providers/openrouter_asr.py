from __future__ import annotations

import asyncio
import base64
import errno
import logging
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from lili_voice_input.config import Settings

logger = logging.getLogger(__name__)


class AsrProviderError(RuntimeError):
    def __init__(
        self,
        error_type: str,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        *,
        provider_code: str | None = None,
        request_id: str | None = None,
        exception_type: str | None = None,
        transport_phase: str | None = None,
        transport_reason: str | None = None,
        os_errno: int | None = None,
    ) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.provider_code = provider_code
        self.request_id = request_id
        self.exception_type = exception_type
        self.transport_phase = transport_phase
        self.transport_reason = transport_reason
        self.os_errno = os_errno


class OpenRouterAsrProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(settings.stt_max_concurrency)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.asr_base_url.rstrip("/"),
                timeout=httpx.Timeout(self.settings.asr_timeout_seconds),
                headers={
                    "Authorization": f"Bearer {self.settings.asr_api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "lili-voice-input",
                },
            )
        return self._client

    async def transcribe(
        self,
        audio: bytes,
        *,
        audio_format: str,
        language: str | None = None,
    ) -> str:
        if not self.settings.asr_api_key.strip() or not self.settings.asr_model.strip():
            raise AsrProviderError("configuration_error")
        payload: dict[str, object] = {
            "model": self.settings.asr_model,
            "input_audio": {
                "data": base64.b64encode(audio).decode("ascii"),
                "format": audio_format,
            },
            "temperature": 0,
        }
        if language:
            payload["language"] = language
        started_at = time.perf_counter()
        trace_state: dict[str, str | None] = {"phase": "request_created"}

        async def record_trace(event_name: str, _info: dict[str, object]) -> None:
            # Closing the response is cancellation cleanup, not the phase that blocked.
            # Preserve the last meaningful send/receive event for timeout diagnosis.
            if ".response_closed." not in event_name:
                trace_state["phase"] = event_name

        try:
            async with self._semaphore:
                response = await self._get_client().post(
                    "/audio/transcriptions",
                    json=payload,
                    extensions={"trace": record_trace},
                )
            response.raise_for_status()
        except asyncio.CancelledError:
            error = AsrProviderError(
                "cancelled",
                exception_type="CancelledError",
                transport_phase=trace_state["phase"],
            )
            log_asr_failure(error, started_at)
            raise
        except httpx.TimeoutException as exc:
            transport_reason, os_errno, cause_type = classify_transport_error(exc)
            error = AsrProviderError(
                "timeout",
                exception_type=type(exc).__name__,
                transport_phase=trace_state["phase"],
                transport_reason=transport_reason,
                os_errno=os_errno,
            )
            if cause_type and cause_type != error.exception_type:
                error.exception_type = f"{error.exception_type}/{cause_type}"
            log_asr_failure(error, started_at)
            raise error from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in {401, 402, 403, 404}:
                error_type = "configuration_error"
            elif code == 429:
                error_type = "rate_limited"
            elif code in {400, 413, 415, 422}:
                error_type = "request_error"
            else:
                error_type = "provider_error"
            retry_after = exc.response.headers.get("retry-after")
            retry_after_seconds = parse_retry_after(retry_after)
            error = AsrProviderError(
                error_type,
                code,
                retry_after_seconds,
                provider_code=extract_provider_code(exc.response),
                request_id=extract_request_id(exc.response.headers),
                exception_type=type(exc).__name__,
            )
            log_asr_failure(error, started_at, exc.response.headers)
            raise error from exc
        except httpx.HTTPError as exc:
            transport_reason, os_errno, cause_type = classify_transport_error(exc)
            error = AsrProviderError(
                "provider_error",
                exception_type=f"{type(exc).__name__}/{cause_type}"
                if cause_type and cause_type != type(exc).__name__
                else type(exc).__name__,
                transport_phase=trace_state["phase"],
                transport_reason=transport_reason,
                os_errno=os_errno,
            )
            log_asr_failure(error, started_at)
            raise error from exc
        try:
            data = response.json()
        except ValueError as exc:
            error = AsrProviderError(
                "provider_error",
                response.status_code,
                provider_code="invalid_json",
                request_id=extract_request_id(response.headers),
                exception_type=type(exc).__name__,
            )
            log_asr_failure(error, started_at, response.headers)
            raise error from exc
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            error = AsrProviderError(
                "request_error",
                response.status_code,
                provider_code="empty_text",
                request_id=extract_request_id(response.headers),
            )
            log_asr_failure(error, started_at, response.headers)
            raise error
        logger.info(
            "ASR_PROVIDER event=success audio_bytes=%s latency_ms=%s",
            len(audio),
            round((time.perf_counter() - started_at) * 1000),
        )
        return text.strip()

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def extract_provider_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else payload.get("code")
    return str(code)[:80] if isinstance(code, (str, int)) else None


def extract_request_id(headers: httpx.Headers) -> str | None:
    for name in ("x-request-id", "openrouter-request-id", "cf-ray"):
        value = headers.get(name)
        if value:
            return value[:128]
    return None


def classify_transport_error(exc: BaseException) -> tuple[str | None, int | None, str | None]:
    """Return a secret-safe transport classification without logging raw exception text."""
    current: BaseException | None = exc
    messages: list[str] = []
    os_error_number: int | None = None
    deepest_type: str | None = None
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        deepest_type = type(current).__name__
        messages.append(str(current).lower())
        if isinstance(current, OSError) and current.errno is not None:
            os_error_number = current.errno
        current = current.__cause__ or current.__context__

    combined = " ".join(messages)
    if os_error_number in {getattr(errno, "EAI_AGAIN", -3), getattr(errno, "EAI_NONAME", -2)} or any(
        marker in combined
        for marker in ("name resolution", "getaddrinfo", "temporary failure in name", "nodename nor servname")
    ):
        reason = "dns_resolution"
    elif "certificate" in combined or "tls" in combined or "ssl" in combined:
        reason = "tls_handshake"
    elif os_error_number == errno.ECONNREFUSED or "connection refused" in combined:
        reason = "tcp_refused"
    elif os_error_number == errno.ECONNRESET or "connection reset" in combined:
        reason = "connection_reset"
    elif os_error_number in {errno.ENETUNREACH, errno.EHOSTUNREACH} or any(
        marker in combined for marker in ("network is unreachable", "no route to host")
    ):
        reason = "network_unreachable"
    elif "timed out" in combined or "timeout" in combined:
        reason = "transport_timeout"
    elif isinstance(exc, httpx.ConnectError):
        reason = "connect_error_unknown"
    else:
        reason = None
    return reason, os_error_number, deepest_type


def log_asr_failure(
    error: AsrProviderError,
    started_at: float,
    headers: httpx.Headers | None = None,
) -> None:
    safe_headers = headers or httpx.Headers()
    logger.warning(
        "ASR_PROVIDER event=failure reason=%s status_code=%s provider_code=%s exception_type=%s "
        "transport_phase=%s transport_reason=%s os_errno=%s request_id=%s retry_after_seconds=%s "
        "rate_limit_limit=%s rate_limit_remaining=%s "
        "rate_limit_reset=%s latency_ms=%s",
        error.error_type,
        error.status_code,
        error.provider_code,
        error.exception_type,
        error.transport_phase,
        error.transport_reason,
        error.os_errno,
        error.request_id,
        error.retry_after_seconds,
        safe_headers.get("x-ratelimit-limit"),
        safe_headers.get("x-ratelimit-remaining"),
        safe_headers.get("x-ratelimit-reset"),
        round((time.perf_counter() - started_at) * 1000),
    )
