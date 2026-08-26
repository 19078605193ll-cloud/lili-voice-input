from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from lili_voice_input.api.auth import require_http_token
from lili_voice_input.api.schemas import TranscriptionResponse
from lili_voice_input.audio.converter import AudioConversionError
from lili_voice_input.providers.openrouter_asr import AsrProviderError
from lili_voice_input.services.capacity import SessionCapacity
from lili_voice_input.services.transcription import TranscriptionService

router = APIRouter(prefix="/v1", tags=["transcription"])
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
    dependencies=[Depends(require_http_token)],
)
async def create_transcription(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form(default="zh"),
) -> TranscriptionResponse:
    content_type = (file.content_type or "").lower().split(";", 1)[0].strip()
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=400, detail="不支持的音频格式")
    maximum = request.app.state.settings.max_upload_bytes
    try:
        content = await file.read(maximum + 1)
    finally:
        await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="音频文件不能为空")
    if len(content) > maximum:
        raise HTTPException(status_code=413, detail=f"音频文件不能超过 {maximum} 字节")

    capacity: SessionCapacity = request.app.state.session_capacity
    if not await capacity.acquire():
        raise HTTPException(status_code=429, detail="语音服务会话已满，请稍后重试")
    service: TranscriptionService = request.app.state.transcription_service
    try:
        result = await service.transcribe_upload(content, language=language or None)
    except AudioConversionError as exc:
        raise conversion_http_error(exc) from exc
    except AsrProviderError as exc:
        raise asr_http_error(exc) from exc
    except ValueError as exc:
        if str(exc) == "empty_audio":
            raise HTTPException(status_code=422, detail="没有检测到有效录音") from exc
        if str(exc) == "max_duration_exceeded":
            raise HTTPException(status_code=422, detail="录音超过最大时长") from exc
        raise HTTPException(status_code=422, detail="音频格式无法处理") from exc
    finally:
        await capacity.release()
    return TranscriptionResponse(**asdict(result))


def asr_http_error(exc: AsrProviderError) -> HTTPException:
    if exc.error_type == "configuration_error":
        return HTTPException(status_code=503, detail="语音转写服务未正确配置")
    if exc.error_type == "rate_limited":
        return HTTPException(status_code=429, detail="语音转写服务繁忙，请稍后重试")
    if exc.error_type == "timeout":
        return HTTPException(status_code=504, detail="语音转写超时，请重试")
    if exc.error_type == "request_error":
        return HTTPException(status_code=422, detail="音频文件无法转写")
    return HTTPException(status_code=502, detail="上游语音转写服务暂时不可用")


def conversion_http_error(exc: AudioConversionError) -> HTTPException:
    if exc.error_type == "dependency_unavailable":
        return HTTPException(status_code=503, detail="FFmpeg 不可用")
    if exc.error_type == "conversion_timeout":
        return HTTPException(status_code=422, detail="音频转换超时")
    if exc.error_type == "duration_exceeded":
        return HTTPException(status_code=422, detail="录音超过最大时长")
    return HTTPException(status_code=422, detail="音频格式无法处理")
