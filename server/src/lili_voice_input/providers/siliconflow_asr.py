from __future__ import annotations

import asyncio
import logging
import time

import httpx

from lili_voice_input.config import Settings
from lili_voice_input.providers.openrouter_asr import (
    AsrProviderError,
    classify_transport_error,
    extract_provider_code,
    extract_request_id,
    log_asr_failure,
    parse_retry_after,
)

logger = logging.getLogger(__name__)


class SiliconFlowAsrProvider:
    """SiliconFlow OpenAI-compatible multipart ASR provider."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.asr_base_url.rstrip("/"),
                timeout=httpx.Timeout(self.settings.asr_timeout_seconds),
                headers={
                    "Authorization": f"Bearer {self.settings.asr_api_key}",
                    "Accept": "application/json",
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

        extension, media_type = audio_media_type(audio_format)
        # SenseVoiceSmall auto-detects language; omit provider-specific optional fields.
        del language
        form = {"model": self.settings.asr_model}
        files = {"file": (f"audio.{extension}", audio, media_type)}
        started_at = time.perf_counter()
        trace_state = {"phase": "request_created"}

        async def record_trace(event_name: str, _info: dict[str, object]) -> None:
            if ".response_closed." not in event_name:
                trace_state["phase"] = event_name

        try:
            response = await self._get_client().post(
                "/audio/transcriptions",
                data=form,
                files=files,
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
                exception_type=f"{type(exc).__name__}/{cause_type}"
                if cause_type and cause_type != type(exc).__name__
                else type(exc).__name__,
                transport_phase=trace_state["phase"],
                transport_reason=transport_reason,
                os_errno=os_errno,
            )
            log_asr_failure(error, started_at)
            raise error from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in {401, 402, 403, 404}:
                error_type = "configuration_error"
            elif status_code == 429:
                error_type = "rate_limited"
            elif status_code in {400, 413, 415, 422}:
                error_type = "request_error"
            else:
                error_type = "provider_error"
            error = AsrProviderError(
                error_type,
                status_code,
                parse_retry_after(exc.response.headers.get("retry-after")),
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
            "ASR_PROVIDER event=success provider=siliconflow audio_bytes=%s latency_ms=%s",
            len(audio),
            round((time.perf_counter() - started_at) * 1000),
        )
        return text.strip()

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()


def audio_media_type(audio_format: str) -> tuple[str, str]:
    normalized = audio_format.casefold().lstrip(".")
    return {
        "wav": ("wav", "audio/wav"),
        "mp3": ("mp3", "audio/mpeg"),
        "m4a": ("m4a", "audio/mp4"),
        "ogg": ("ogg", "audio/ogg"),
        "flac": ("flac", "audio/flac"),
    }.get(normalized, (normalized or "wav", f"audio/{normalized or 'wav'}"))
