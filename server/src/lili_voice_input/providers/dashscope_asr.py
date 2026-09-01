from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from uuid import uuid4

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

_TERMINAL_TASK_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}
_PARAFORMER_MODELS = {"paraformer-v1", "paraformer-v2", "paraformer-8k-v1"}


@dataclass(frozen=True)
class UploadCredential:
    upload_host: str
    upload_dir: str
    oss_access_key_id: str = dataclass_field(repr=False)
    signature: str = dataclass_field(repr=False)
    policy: str = dataclass_field(repr=False)
    object_acl: str = dataclass_field(repr=False)
    forbid_overwrite: str = dataclass_field(repr=False)
    expires_at: float = dataclass_field(repr=False)


class DashScopeAsrProvider:
    """Alibaba Cloud Model Studio asynchronous file-transcription provider."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        result_client: httpx.AsyncClient | None = None,
        upload_client: httpx.AsyncClient | None = None,
        *,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._result_client = result_client
        self._upload_client = upload_client
        self._owns_client = client is None
        self._owns_result_client = result_client is None
        self._owns_upload_client = upload_client is None
        self._poll_interval_seconds = poll_interval_seconds
        self._upload_credential: UploadCredential | None = None
        self._upload_credential_lock = asyncio.Lock()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.asr_base_url.rstrip("/") + "/",
                timeout=httpx.Timeout(self.settings.asr_timeout_seconds),
                headers={
                    "Authorization": f"Bearer {self.settings.asr_api_key}",
                    "Accept": "application/json; charset=utf-8",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        return self._client

    def _get_result_client(self) -> httpx.AsyncClient:
        if self._result_client is None:
            # The signed result URL may use an OSS domain. Never forward the DashScope key to it.
            self._result_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.asr_timeout_seconds),
                follow_redirects=True,
            )
        return self._result_client

    def _get_upload_client(self) -> httpx.AsyncClient:
        if self._upload_client is None:
            # OSS receives only its temporary multipart fields, never the DashScope bearer token.
            self._upload_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.asr_timeout_seconds),
                follow_redirects=True,
            )
        return self._upload_client

    async def transcribe(
        self,
        audio: bytes,
        *,
        audio_format: str,
        language: str | None = None,
    ) -> str:
        del language  # The file-transcription model auto-detects the spoken language.
        if not self.settings.asr_api_key.strip() or not self.settings.asr_model.strip():
            raise AsrProviderError("configuration_error")

        started_at = time.perf_counter()
        deadline = started_at + self.settings.asr_timeout_seconds
        credential_ms = 0
        upload_ms = 0
        if is_paraformer_model(self.settings.asr_model):
            credential_started_at = time.perf_counter()
            credential = await self._get_upload_credential(started_at, deadline)
            credential_ms = round((time.perf_counter() - credential_started_at) * 1000)
            upload_started_at = time.perf_counter()
            audio_url = await self._upload_audio(
                audio,
                audio_format=audio_format,
                credential=credential,
                started_at=started_at,
                deadline=deadline,
            )
            upload_ms = round((time.perf_counter() - upload_started_at) * 1000)
            submit_input = {"file_urls": [audio_url]}
        else:
            encoded_audio = base64.b64encode(audio).decode("ascii")
            submit_input = {"file_url": f"data:{audio_media_type(audio_format)};base64,{encoded_audio}"}
        submit_payload = {
            "model": self.settings.asr_model,
            "input": submit_input,
        }
        submit_started_at = time.perf_counter()
        data, response = await self._request_json(
            self._get_client(),
            "POST",
            "services/audio/asr/transcription",
            started_at=started_at,
            deadline=deadline,
            stage="submit",
            json=submit_payload,
            headers={"X-DashScope-Async": "enable"},
        )
        submit_ms = round((time.perf_counter() - submit_started_at) * 1000)
        output = extract_task_output(data)
        task_id = output.get("task_id") if output else None
        if not isinstance(task_id, str) or not task_id:
            self._raise_invalid_response("missing_task_id", response, data, started_at)

        poll_count = 0
        poll_wait_seconds = 0.0
        while True:
            status = str(output.get("task_status", "")).upper()
            if status in _TERMINAL_TASK_STATES:
                break
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                error = AsrProviderError(
                    "timeout",
                    provider_code="task_poll_timeout",
                    request_id=response_request_id(response, data),
                    transport_phase="poll",
                )
                log_asr_failure(error, started_at)
                raise error
            delay = min(self._poll_delay(poll_count), remaining)
            wait_started_at = time.perf_counter()
            await asyncio.sleep(delay)
            poll_wait_seconds += time.perf_counter() - wait_started_at
            data, response = await self._request_json(
                self._get_client(),
                "GET",
                f"tasks/{task_id}",
                started_at=started_at,
                deadline=deadline,
                stage="poll",
            )
            poll_count += 1
            output = extract_task_output(data)
            if output is None:
                self._raise_invalid_response("missing_task_output", response, data, started_at)

        if status != "SUCCEEDED":
            provider_code = task_failure_code(output, data, status)
            error_type = "rate_limited" if is_rate_limit_code(provider_code) else "provider_error"
            error = AsrProviderError(
                error_type,
                response.status_code,
                provider_code=provider_code,
                request_id=response_request_id(response, data),
            )
            log_asr_failure(error, started_at, response.headers)
            raise error

        transcription_url = extract_transcription_url(output)
        if transcription_url is None:
            self._raise_invalid_response("missing_transcription_url", response, data, started_at)
        result_fetch_started_at = time.perf_counter()
        result_data, result_response = await self._request_json(
            self._get_result_client(),
            "GET",
            transcription_url,
            started_at=started_at,
            deadline=deadline,
            stage="result_fetch",
        )
        result_fetch_ms = round((time.perf_counter() - result_fetch_started_at) * 1000)
        text = extract_transcript(result_data)
        if text is None:
            self._raise_invalid_response("empty_text", result_response, result_data, started_at)

        logger.info(
            "ASR_PROVIDER event=success provider=dashscope model_family=%s audio_bytes=%s "
            "credential_ms=%s upload_ms=%s submit_ms=%s poll_count=%s poll_wait_ms=%s "
            "result_fetch_ms=%s latency_ms=%s request_id=%s",
            "paraformer" if is_paraformer_model(self.settings.asr_model) else "qwen",
            len(audio),
            credential_ms,
            upload_ms,
            submit_ms,
            poll_count,
            round(poll_wait_seconds * 1000),
            result_fetch_ms,
            round((time.perf_counter() - started_at) * 1000),
            response_request_id(response, data),
        )
        return text

    def _poll_delay(self, _poll_count: int) -> float:
        if self._poll_interval_seconds is not None:
            return max(0.0, self._poll_interval_seconds)
        return 1.0

    async def _get_upload_credential(self, started_at: float, deadline: float) -> UploadCredential:
        cached = self._upload_credential
        if cached is not None and cached.expires_at > time.monotonic():
            return cached
        async with self._upload_credential_lock:
            cached = self._upload_credential
            if cached is not None and cached.expires_at > time.monotonic():
                return cached
            data, response = await self._request_json(
                self._get_client(),
                "GET",
                "uploads",
                started_at=started_at,
                deadline=deadline,
                stage="upload_credential",
                params={"action": "getPolicy", "model": self.settings.asr_model},
            )
            try:
                credential = extract_upload_credential(data)
            except ValueError:
                self._raise_invalid_response("invalid_upload_credential", response, data, started_at)
            self._upload_credential = credential
            return credential

    async def _invalidate_upload_credential(self, credential: UploadCredential) -> None:
        async with self._upload_credential_lock:
            if self._upload_credential is credential:
                self._upload_credential = None

    async def _upload_audio(
        self,
        audio: bytes,
        *,
        audio_format: str,
        credential: UploadCredential,
        started_at: float,
        deadline: float,
    ) -> str:
        current = credential
        for attempt in range(2):
            try:
                return await self._upload_once(
                    audio,
                    audio_format=audio_format,
                    credential=current,
                    started_at=started_at,
                    deadline=deadline,
                )
            except httpx.HTTPStatusError as exc:
                if attempt == 0 and exc.response.status_code in {400, 403}:
                    await self._invalidate_upload_credential(current)
                    current = await self._get_upload_credential(started_at, deadline)
                    continue
                error = upload_status_error(exc.response, started_at)
                raise error from exc
        raise AssertionError("unreachable")

    async def _upload_once(
        self,
        audio: bytes,
        *,
        audio_format: str,
        credential: UploadCredential,
        started_at: float,
        deadline: float,
    ) -> str:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            error = AsrProviderError("timeout", provider_code="upload_timeout", transport_phase="upload")
            log_asr_failure(error, started_at)
            raise error
        extension = normalized_audio_extension(audio_format)
        object_key = f"{credential.upload_dir.rstrip('/')}/{uuid4().hex}.{extension}"
        form = {
            "OSSAccessKeyId": credential.oss_access_key_id,
            "Signature": credential.signature,
            "policy": credential.policy,
            "key": object_key,
            "x-oss-object-acl": credential.object_acl,
            "x-oss-forbid-overwrite": credential.forbid_overwrite,
        }
        trace_state: dict[str, str] = {"phase": "upload"}

        async def record_trace(event_name: str, _info: dict[str, object]) -> None:
            if ".response_closed." not in event_name:
                trace_state["phase"] = f"upload:{event_name}"

        try:
            response = await self._get_upload_client().post(
                credential.upload_host,
                data=form,
                files={"file": (f"audio.{extension}", audio, audio_media_type(extension))},
                timeout=remaining,
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
        except httpx.HTTPStatusError:
            raise
        except httpx.TimeoutException as exc:
            transport_reason, os_errno, cause_type = classify_transport_error(exc)
            error = AsrProviderError(
                "timeout",
                provider_code="upload_timeout",
                exception_type=exception_chain_name(exc, cause_type),
                transport_phase=trace_state["phase"],
                transport_reason=transport_reason,
                os_errno=os_errno,
            )
            log_asr_failure(error, started_at)
            raise error from exc
        except httpx.HTTPError as exc:
            transport_reason, os_errno, cause_type = classify_transport_error(exc)
            error = AsrProviderError(
                "provider_error",
                provider_code="upload_transport_error",
                exception_type=exception_chain_name(exc, cause_type),
                transport_phase=trace_state["phase"],
                transport_reason=transport_reason,
                os_errno=os_errno,
            )
            log_asr_failure(error, started_at)
            raise error from exc
        return f"oss://{object_key}"

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        started_at: float,
        deadline: float,
        stage: str,
        **kwargs: object,
    ) -> tuple[dict[str, object], httpx.Response]:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            error = AsrProviderError("timeout", provider_code=f"{stage}_timeout", transport_phase=stage)
            log_asr_failure(error, started_at)
            raise error
        trace_state: dict[str, str] = {"phase": stage}

        async def record_trace(event_name: str, _info: dict[str, object]) -> None:
            if ".response_closed." not in event_name:
                trace_state["phase"] = f"{stage}:{event_name}"

        try:
            response = await client.request(
                method,
                url,
                timeout=remaining,
                extensions={"trace": record_trace},
                **kwargs,
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
                provider_code=f"{stage}_timeout",
                exception_type=exception_chain_name(exc, cause_type),
                transport_phase=trace_state["phase"],
                transport_reason=transport_reason,
                os_errno=os_errno,
            )
            log_asr_failure(error, started_at)
            raise error from exc
        except httpx.HTTPStatusError as exc:
            error = http_status_error(exc.response, started_at)
            raise error from exc
        except httpx.HTTPError as exc:
            transport_reason, os_errno, cause_type = classify_transport_error(exc)
            error = AsrProviderError(
                "provider_error",
                provider_code=f"{stage}_transport_error",
                exception_type=exception_chain_name(exc, cause_type),
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
                provider_code=f"{stage}_invalid_json",
                request_id=extract_request_id(response.headers),
                exception_type=type(exc).__name__,
            )
            log_asr_failure(error, started_at, response.headers)
            raise error from exc
        if not isinstance(data, dict):
            self._raise_invalid_response(f"{stage}_invalid_json_type", response, {}, started_at)
        return data, response

    def _raise_invalid_response(
        self,
        provider_code: str,
        response: httpx.Response,
        data: dict[str, object],
        started_at: float,
    ) -> None:
        error = AsrProviderError(
            "request_error",
            response.status_code,
            provider_code=provider_code,
            request_id=response_request_id(response, data),
        )
        log_asr_failure(error, started_at, response.headers)
        raise error

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        if self._result_client is not None and self._owns_result_client:
            await self._result_client.aclose()
        if self._upload_client is not None and self._owns_upload_client:
            await self._upload_client.aclose()


def is_paraformer_model(model: str) -> bool:
    normalized = model.casefold().strip()
    return normalized in _PARAFORMER_MODELS or normalized.startswith("paraformer-")


def normalized_audio_extension(audio_format: str) -> str:
    normalized = audio_format.casefold().strip().lstrip(".")
    return normalized or "wav"


def audio_media_type(audio_format: str) -> str:
    normalized = normalized_audio_extension(audio_format)
    return {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "ogg": "audio/ogg",
        "flac": "audio/flac",
    }.get(normalized, f"audio/{normalized}")


def extract_upload_credential(payload: object) -> UploadCredential:
    if not isinstance(payload, dict):
        raise TypeError("invalid upload credential payload")
    candidate = payload.get("data")
    if not isinstance(candidate, dict):
        candidate = payload.get("output")
    if not isinstance(candidate, dict):
        raise TypeError("missing upload credential data")

    def required_string(name: str) -> str:
        value = candidate.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"missing {name}")
        return value.strip()

    expires_in = candidate.get("expire_in_seconds", 300)
    if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool) or expires_in <= 0:
        expires_in = 300
    refresh_margin = min(30.0, float(expires_in) / 2)
    upload_dir = required_string("upload_dir").strip("/")
    if not upload_dir:
        raise ValueError("empty upload_dir")
    return UploadCredential(
        upload_host=required_string("upload_host"),
        upload_dir=upload_dir,
        oss_access_key_id=required_string("oss_access_key_id"),
        signature=required_string("signature"),
        policy=required_string("policy"),
        object_acl=str(candidate.get("x_oss_object_acl") or "private"),
        forbid_overwrite=str(candidate.get("x_oss_forbid_overwrite") or "true").lower(),
        expires_at=time.monotonic() + max(1.0, float(expires_in) - refresh_margin),
    )


def extract_task_output(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    return output if isinstance(output, dict) else None


def extract_transcription_url(output: dict[str, object]) -> str | None:
    direct = output.get("transcription_url")
    if isinstance(direct, str) and direct:
        return direct
    results = output.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        nested = results[0].get("transcription_url")
        if isinstance(nested, str) and nested:
            return nested
    return None


def extract_transcript(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    transcripts = payload.get("transcripts")
    if not isinstance(transcripts, list):
        return None
    parts = [
        item.get("text", "").strip()
        for item in transcripts
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    text = "\n".join(part for part in parts if part)
    return text or None


def response_request_id(response: httpx.Response, payload: dict[str, object]) -> str | None:
    header_request_id = extract_request_id(response.headers)
    if header_request_id:
        return header_request_id
    request_id = payload.get("request_id")
    return request_id[:128] if isinstance(request_id, str) and request_id else None


def task_failure_code(output: dict[str, object], payload: dict[str, object], status: str) -> str:
    for source in (output, payload):
        code = source.get("code")
        if isinstance(code, (str, int)):
            return str(code)[:80]
    return f"task_{status.casefold()}"


def is_rate_limit_code(code: str) -> bool:
    normalized = code.casefold()
    return any(marker in normalized for marker in ("throttl", "rate", "quota", "limit"))


def http_status_error(
    response: httpx.Response,
    started_at: float,
    *,
    provider_code: str | None = None,
) -> AsrProviderError:
    status_code = response.status_code
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
        parse_retry_after(response.headers.get("retry-after")),
        provider_code=provider_code or extract_provider_code(response),
        request_id=extract_request_id(response.headers),
        exception_type="HTTPStatusError",
    )
    log_asr_failure(error, started_at, response.headers)
    return error


def upload_status_error(response: httpx.Response, started_at: float) -> AsrProviderError:
    status_code = response.status_code
    if status_code == 429:
        error_type = "rate_limited"
    elif status_code in {400, 413, 415, 422}:
        error_type = "request_error"
    else:
        # The main API already issued credentials, so a repeated OSS rejection is an upstream upload failure.
        error_type = "provider_error"
    error = AsrProviderError(
        error_type,
        status_code,
        parse_retry_after(response.headers.get("retry-after")),
        provider_code="oss_upload_rejected",
        request_id=extract_request_id(response.headers),
        exception_type="HTTPStatusError",
        transport_phase="upload",
    )
    log_asr_failure(error, started_at, response.headers)
    return error


def exception_chain_name(exc: BaseException, cause_type: str | None) -> str:
    name = type(exc).__name__
    return f"{name}/{cause_type}" if cause_type and cause_type != name else name
