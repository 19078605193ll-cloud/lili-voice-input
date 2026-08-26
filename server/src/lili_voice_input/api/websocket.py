from __future__ import annotations

from dataclasses import asdict

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from lili_voice_input.api.auth import token_matches
from lili_voice_input.audio.pcm import SAMPLE_RATE
from lili_voice_input.config import Settings
from lili_voice_input.providers.openrouter_asr import AsrProviderError
from lili_voice_input.services.capacity import SessionCapacity
from lili_voice_input.services.polishing import PolishingService
from lili_voice_input.services.streaming import StreamingSession

router = APIRouter(prefix="/v1", tags=["transcription"])
logger = logging.getLogger(__name__)
PROTOCOL_VERSION = "1"


@router.websocket("/transcriptions/stream")
async def stream_transcription(websocket: WebSocket) -> None:
    settings: Settings = websocket.app.state.settings
    origin = websocket.headers.get("origin")
    if origin and "*" not in settings.allowed_origins and origin.rstrip("/") not in settings.allowed_origins:
        await websocket.close(code=1008, reason="Origin not allowed")
        return
    capacity: SessionCapacity = websocket.app.state.session_capacity
    if not await capacity.acquire():
        await websocket.close(code=1013, reason="Server session capacity reached")
        return
    await websocket.accept()
    session: StreamingSession | None = None
    started = False
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            pcm = message.get("bytes")
            if pcm is not None:
                if not started or session is None:
                    await send_error(websocket, "INVALID_STATE", "语音会话尚未开始", False)
                    break
                try:
                    await session.add_audio(pcm)
                except ValueError as exc:
                    code = "MAX_DURATION" if str(exc) == "max_duration_exceeded" else "INVALID_AUDIO"
                    detail = "录音超过最大时长" if code == "MAX_DURATION" else "音频数据无法处理"
                    await send_error(websocket, code, detail, False)
                    break
                continue

            raw = message.get("text")
            if raw is None:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                await send_error(websocket, "INVALID_EVENT", "语音控制消息格式错误", True)
                continue
            event_type = event.get("type")
            if event_type == "start":
                if started:
                    await send_error(websocket, "INVALID_STATE", "语音会话已经开始", True)
                    continue
                if not token_matches(settings, event.get("auth_token")):
                    await send_error(websocket, "UNAUTHORIZED", "访问令牌无效", False)
                    break
                if event.get("protocol_version") != PROTOCOL_VERSION:
                    await send_error(websocket, "UNSUPPORTED_PROTOCOL", "仅支持协议版本 1", False)
                    break
                if event.get("sample_rate") != SAMPLE_RATE or event.get("format") != "pcm16":
                    await send_error(websocket, "UNSUPPORTED_AUDIO", "仅支持 16kHz PCM16 音频", False)
                    break
                session = StreamingSession(
                    asr_provider=websocket.app.state.asr_provider,
                    polish_service=websocket.app.state.polishing_service,
                    language=event.get("language") or "zh",
                    **settings.stream_options(),
                )
                started = True
                await websocket.send_json(
                    {
                        "type": "ready",
                        "protocol_version": PROTOCOL_VERSION,
                        "session_id": session.session_id,
                        "sample_rate": SAMPLE_RATE,
                        "max_duration_seconds": settings.stt_max_duration_seconds,
                        "capabilities": {"partial": False, "http_fallback": True},
                    }
                )
                continue
            if event_type == "commit":
                if not started or session is None:
                    await send_error(websocket, "INVALID_STATE", "语音会话尚未开始", False)
                    break
                try:
                    result = await session.finalize()
                except ValueError as exc:
                    code = "EMPTY_AUDIO" if str(exc) == "empty_audio" else "INVALID_STATE"
                    detail = "没有检测到有效录音" if code == "EMPTY_AUDIO" else "语音会话无法完成"
                    await send_error(websocket, code, detail, False)
                except AsrProviderError as exc:
                    await send_asr_error(websocket, exc)
                else:
                    await websocket.send_json({"type": "final", **asdict(result)})
                break
            if event_type == "cancel":
                break
            await send_error(websocket, "INVALID_EVENT", "不支持的语音控制消息", True)
    except WebSocketDisconnect:
        logger.info("STREAM event=client_disconnected session=%s", session.session_id if session else "unstarted")
    finally:
        if session is not None:
            await session.close()
        await capacity.release()
        try:
            await websocket.close()
        except RuntimeError:
            pass


async def send_asr_error(websocket: WebSocket, exc: AsrProviderError) -> None:
    mapping = {
        "configuration_error": ("CONFIGURATION_ERROR", "语音转写服务未正确配置", False),
        "rate_limited": ("RATE_LIMITED", "语音转写服务繁忙，请稍后重试", True),
        "timeout": ("ASR_TIMEOUT", "语音最终识别超时", True),
    }
    code, message, recoverable = mapping.get(exc.error_type, ("ASR_PROVIDER_ERROR", "上游语音服务暂时不可用", True))
    await send_error(websocket, code, message, recoverable)


async def send_error(websocket: WebSocket, code: str, message: str, recoverable: bool) -> None:
    await websocket.send_json({"type": "error", "code": code, "message": message, "recoverable": recoverable})
