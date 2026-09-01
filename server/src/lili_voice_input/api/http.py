from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from lili_voice_input.api.auth import Principal, require_http_token
from lili_voice_input.api.schemas import TranscriptionResponse
from lili_voice_input.audio.converter import AudioConversionError
from lili_voice_input.providers.openrouter_asr import AsrProviderError
from lili_voice_input.services import metrics
from lili_voice_input.services.admission import AdmissionRejected
from lili_voice_input.services.runtime import RuntimeUnavailable
from lili_voice_input.services.transcription import TranscriptionService

router = APIRouter(prefix="/v1", tags=["transcription"])
logger = logging.getLogger(__name__)
ALLOWED_AUDIO_TYPES = {
    "audio/webm",
    "audio/mp4",
    "audio/x-m4a",
    "audio/m4a",
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/ogg",
    "audio/aac",
    "audio/flac",
}


@router.post(
    "/transcriptions",
    response_model=TranscriptionResponse,
)
async def create_transcription(
    request: Request,
    file: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require_http_token)],
    language: Annotated[str, Form()] = "zh",
) -> TranscriptionResponse:
    content_type = (file.content_type or "").lower().split(";", 1)[0].strip()
    if content_type not in ALLOWED_AUDIO_TYPES:
        await file.close()
        raise HTTPException(status_code=400, detail="不支持的音频格式")
    admission = request.app.state.admission
    subject = principal.subject if not principal.trusted else f"trusted:{uuid.uuid4().hex}"
    try:
        lease = await admission.acquire(subject, "http")
    except AdmissionRejected as exc:
        error_code = "QUEUE_TIMEOUT" if exc.reason == "queue_timeout" else "CAPACITY_REACHED"
        raise HTTPException(
            status_code=429,
            detail="语音服务会话已满，请稍后重试",
            headers={
                "Retry-After": str(max(1, exc.retry_after_ms // 1000)),
                "X-Retry-After-Ms": str(exc.retry_after_ms),
                "X-Error-Code": error_code,
            },
        ) from exc
    except RuntimeUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="容量控制服务不可用",
            headers={"Retry-After": "5", "X-Retry-After-Ms": "5000", "X-Error-Code": "CAPACITY_REACHED"},
        ) from exc
    maximum = request.app.state.settings.max_upload_bytes
    logger.info(
        "ADMISSION event=granted subject=%s transport=http wait_ms=%s",
        subject,
        lease.wait_ms,
    )
    started_at = time.perf_counter()
    is_fallback = request.headers.get("x-voice-fallback") == "1"
    outcome_recorded = False
    if is_fallback:
        metrics.HTTP_FALLBACKS.labels("started").inc()
    try:
        try:
            content = await file.read(maximum + 1)
        finally:
            await file.close()
        if not content:
            raise HTTPException(status_code=400, detail="音频文件不能为空")
        if len(content) > maximum:
            raise HTTPException(status_code=413, detail=f"音频文件不能超过 {maximum} 字节")
        service: TranscriptionService = request.app.state.transcription_service
        result = await service.transcribe_upload(
            content,
            language=language or None,
            admission_wait_ms=lease.wait_ms,
        )
        metrics.FINAL_LATENCY.labels("http").observe(time.perf_counter() - started_at)
        metrics.SESSION_OUTCOMES.labels("success", "http").inc()
        outcome_recorded = True
        logger.info(
            "FINAL event=success subject=%s transport=http admission_wait_ms=%s asr_queue_wait_ms=%s "
            "total_latency_ms=%s degraded_stage=%s",
            subject,
            result.admission_wait_ms,
            result.asr_queue_wait_ms,
            result.total_latency_ms,
            result.degraded_stage,
        )
        if result.degraded:
            metrics.DEGRADED_RESULTS.labels(result.degraded_stage or "unknown").inc()
        if is_fallback:
            metrics.HTTP_FALLBACKS.labels("success").inc()
        return TranscriptionResponse(**asdict(result))
    except AudioConversionError as exc:
        metrics.SESSION_OUTCOMES.labels(exc.error_type, "http").inc()
        outcome_recorded = True
        raise conversion_http_error(exc) from exc
    except AsrProviderError as exc:
        metrics.SESSION_OUTCOMES.labels(exc.error_type, "http").inc()
        outcome_recorded = True
        raise asr_http_error(exc) from exc
    except ValueError as exc:
        metrics.SESSION_OUTCOMES.labels(str(exc), "http").inc()
        outcome_recorded = True
        if str(exc) == "empty_audio":
            raise HTTPException(
                status_code=422,
                detail="没有检测到语音，或说话时间太短，请重试",
                headers={"X-Error-Code": "EMPTY_AUDIO"},
            ) from exc
        if str(exc) in {"max_duration_exceeded", "session_wall_timeout"}:
            raise HTTPException(status_code=422, detail="录音超过最大时长") from exc
        raise HTTPException(status_code=422, detail="音频格式无法处理") from exc
    finally:
        if not outcome_recorded:
            metrics.SESSION_OUTCOMES.labels("request_failed", "http").inc()
        if is_fallback and "result" not in locals():
            metrics.HTTP_FALLBACKS.labels("failure").inc()
        await lease.release()


def asr_http_error(exc: AsrProviderError) -> HTTPException:
    if exc.error_type == "configuration_error":
        return HTTPException(status_code=503, detail="语音转写服务未正确配置")
    if exc.error_type == "rate_limited":
        retry_seconds = max(1, round(exc.retry_after_seconds or 5))
        return HTTPException(
            status_code=429,
            detail="语音转写服务繁忙，请稍后重试",
            headers={
                "Retry-After": str(retry_seconds),
                "X-Retry-After-Ms": str(retry_seconds * 1000),
                "X-Error-Code": "RATE_LIMITED",
            },
        )
    if exc.error_type == "timeout":
        return HTTPException(status_code=504, detail="语音转写超时，请重试")
    if exc.error_type == "queue_timeout":
        return HTTPException(
            status_code=429,
            detail="语音转写队列繁忙，请稍后重试",
            headers={"Retry-After": "5", "X-Retry-After-Ms": "5000", "X-Error-Code": "QUEUE_TIMEOUT"},
        )
    if exc.error_type == "request_error":
        return HTTPException(status_code=422, detail="音频文件无法转写")
    return HTTPException(status_code=502, detail="上游语音转写服务暂时不可用")


def conversion_http_error(exc: AudioConversionError) -> HTTPException:
    if exc.error_type == "dependency_unavailable":
        return HTTPException(status_code=503, detail="FFmpeg 不可用")
    if exc.error_type == "conversion_timeout":
        return HTTPException(status_code=422, detail="音频转换超时")
    if exc.error_type == "capacity_reached":
        return HTTPException(
            status_code=429,
            detail="音频转换服务繁忙，请稍后重试",
            headers={"Retry-After": "5", "X-Retry-After-Ms": "5000", "X-Error-Code": "CAPACITY_REACHED"},
        )
    if exc.error_type == "duration_exceeded":
        return HTTPException(status_code=422, detail="录音超过最大时长")
    return HTTPException(status_code=422, detail="音频格式无法处理")
