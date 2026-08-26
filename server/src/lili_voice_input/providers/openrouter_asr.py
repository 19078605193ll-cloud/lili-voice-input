from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx

from lili_voice_input.config import Settings

logger = logging.getLogger(__name__)


class AsrProviderError(RuntimeError):
    def __init__(self, error_type: str, status_code: int | None = None) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.status_code = status_code


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
        try:
            async with self._semaphore:
                response = await self._get_client().post("/audio/transcriptions", json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AsrProviderError("timeout") from exc
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
            raise AsrProviderError(error_type, code) from exc
        except httpx.HTTPError as exc:
            raise AsrProviderError("provider_error") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise AsrProviderError("provider_error") from exc
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise AsrProviderError("request_error")
        logger.info(
            "ASR_PROVIDER event=success audio_bytes=%s latency_ms=%s",
            len(audio),
            round((time.perf_counter() - started_at) * 1000),
        )
        return text.strip()

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
