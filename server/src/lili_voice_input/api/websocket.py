from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from lili_voice_input.api.auth import AnonymousTokenService, client_ip
from lili_voice_input.audio.pcm import SAMPLE_RATE
from lili_voice_input.config import Settings
from lili_voice_input.providers.openrouter_asr import AsrProviderError
from lili_voice_input.services import metrics
from lili_voice_input.services.admission import AdmissionLease, AdmissionRejected
from lili_voice_input.services.runtime import RuntimeUnavailable
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
    await websocket.accept()
    session: StreamingSession | None = None
    lease: AdmissionLease | None = None
    session_outcome: str | None = None
    close_code = 1000
    started_at = time.monotonic()
    try:
        try:
            first_message = await asyncio.wait_for(websocket.receive(), timeout=settings.stt_start_timeout_seconds)
        except TimeoutError:
            metrics.WS_DISCONNECTS.labels("start_timeout").inc()
            await send_error(websocket, "START_TIMEOUT", "语音会话启动超时", True, 1000)
            return
        raw_start = first_message.get("text")
        if raw_start is None:
            await send_error(websocket, "INVALID_EVENT", "首个事件必须是 start", False)
            return
        try:
            event = json.loads(raw_start)
        except json.JSONDecodeError:
            await send_error(websocket, "INVALID_EVENT", "语音控制消息格式错误", False)
            return
        if event.get("type") != "start":
            await send_error(websocket, "INVALID_EVENT", "首个事件必须是 start", False)
            return
        if event.get("protocol_version") != PROTOCOL_VERSION:
            await send_error(websocket, "UNSUPPORTED_PROTOCOL", "仅支持协议版本 1", False)
            return
        if event.get("sample_rate") != SAMPLE_RATE or event.get("format") != "pcm16":
            await send_error(websocket, "UNSUPPORTED_AUDIO", "仅支持 16kHz PCM16 音频", False)
            return
        token_service: AnonymousTokenService = websocket.app.state.token_service
        principal = token_service.authenticate(event.get("auth_token"), origin)
        if principal is None:
            await send_error(websocket, "UNAUTHORIZED", "访问令牌无效", False)
            return
        ip = client_ip(websocket.client, websocket.headers, settings)
        try:
            allowed = await token_service.allow_session_start(principal, ip)
        except RuntimeUnavailable:
            await send_error(websocket, "CAPACITY_REACHED", "容量控制服务不可用", True, 5000)
            close_code = 1013
            return
        if not allowed:
            await send_error(websocket, "RATE_LIMITED", "语音请求过于频繁，请稍后重试", True, 60_000)
            close_code = 1013
            return
        if getattr(websocket.app.state, "draining", False):
            await send_error(websocket, "SERVER_RESTART", "服务正在更新，请重新连接", True, 1000)
            close_code = 1012
            return

        async def queued(position: int, estimated_wait_ms: int) -> None:
            await websocket.send_json(
                {
                    "type": "queued",
                    "position": position,
                    "estimated_wait_ms": estimated_wait_ms,
                    "max_wait_ms": round(settings.stt_admission_wait_seconds * 1000),
                }
            )

        subject = principal.subject if not principal.trusted else f"trusted:{uuid.uuid4().hex}"
        try:
            lease = await websocket.app.state.admission.acquire(subject, "websocket", queued)
        except AdmissionRejected as exc:
            code = "QUEUE_TIMEOUT" if exc.reason == "queue_timeout" else "CAPACITY_REACHED"
            await send_error(websocket, code, "语音服务繁忙，请稍后重试", True, exc.retry_after_ms)
            close_code = 1013
            return
        except RuntimeUnavailable:
            await send_error(websocket, "CAPACITY_REACHED", "容量控制服务不可用", True, 5000)
            close_code = 1013
            return

        asr_provider = websocket.app.state.asr_provider
        asr_scheduler = websocket.app.state.asr_scheduler
        if asr_scheduler.provider is not asr_provider:
            asr_scheduler = None
        session = StreamingSession(
            asr_provider=asr_provider,
            asr_scheduler=asr_scheduler,
            polish_service=websocket.app.state.polishing_service,
            language=event.get("language") or "zh",
            admission_wait_ms=lease.wait_ms,
            **settings.stream_options(),
        )
        logger.info(
            "ADMISSION event=granted session=%s subject=%s transport=websocket wait_ms=%s",
            session.session_id,
            subject,
            lease.wait_ms,
        )
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
        await websocket.app.state.connection_registry.add(websocket)
        while True:
            wall_remaining = settings.stt_session_wall_timeout_seconds - (time.monotonic() - started_at)
            if wall_remaining <= 0:
                session_outcome = "max_duration"
                await send_error(websocket, "MAX_DURATION", "语音会话超过最大持续时间", False)
                break
            try:
                message = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=min(settings.stt_idle_timeout_seconds, wall_remaining),
                )
            except TimeoutError:
                session_outcome = "idle_timeout"
                metrics.WS_DISCONNECTS.labels("idle_timeout").inc()
                await send_error(websocket, "IDLE_TIMEOUT", "语音连接长时间未收到数据", True, 1000)
                break
            if message.get("type") == "websocket.disconnect":
                session_outcome = "disconnected"
                break
            pcm = message.get("bytes")
            if pcm is not None:
                try:
                    await session.add_audio(pcm)
                except ValueError as exc:
                    code = (
                        "MAX_DURATION"
                        if str(exc) in {"max_duration_exceeded", "session_wall_timeout"}
                        else "INVALID_AUDIO"
                    )
                    detail = "录音超过最大时长" if code == "MAX_DURATION" else "音频数据无法处理"
                    session_outcome = code.lower()
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
                await send_error(websocket, "INVALID_STATE", "语音会话已经开始", True)
                continue
            if event_type == "commit":
                final_started = time.perf_counter()
                try:
                    result = await session.finalize()
                except ValueError as exc:
                    code = "EMPTY_AUDIO" if str(exc) == "empty_audio" else "INVALID_STATE"
                    detail = (
                        "没有检测到语音，或说话时间太短，请重试"
                        if code == "EMPTY_AUDIO"
                        else "语音会话无法完成"
                    )
                    session_outcome = code.lower()
                    await send_error(websocket, code, detail, False)
                except AsrProviderError as exc:
                    session_outcome = exc.error_type
                    await send_asr_error(websocket, exc)
                else:
                    await websocket.send_json({"type": "final", **asdict(result)})
                    metrics.FINAL_LATENCY.labels("websocket").observe(time.perf_counter() - final_started)
                    session_outcome = "success"
                    logger.info(
                        "FINAL event=success session=%s admission_wait_ms=%s asr_queue_wait_ms=%s "
                        "total_latency_ms=%s degraded_stage=%s",
                        session.session_id,
                        result.admission_wait_ms,
                        result.asr_queue_wait_ms,
                        result.total_latency_ms,
                        result.degraded_stage,
                    )
                    if result.degraded:
                        metrics.DEGRADED_RESULTS.labels(result.degraded_stage or "unknown").inc()
                break
            if event_type == "cancel":
                session_outcome = "cancelled"
                break
            await send_error(websocket, "INVALID_EVENT", "不支持的语音控制消息", True)
    except WebSocketDisconnect:
        if lease is not None:
            session_outcome = "disconnected"
        metrics.WS_DISCONNECTS.labels("client").inc()
        logger.info("STREAM event=client_disconnected session=%s", session.session_id if session else "unstarted")
    finally:
        registry = getattr(websocket.app.state, "connection_registry", None)
        if registry is not None:
            await registry.remove(websocket)
        if session is not None:
            await session.close()
        if lease is not None:
            metrics.SESSION_OUTCOMES.labels(session_outcome or "disconnected", "websocket").inc()
            await lease.release()
        try:
            await websocket.close(code=close_code)
        except RuntimeError:
            pass


async def send_asr_error(websocket: WebSocket, exc: AsrProviderError) -> None:
    mapping = {
        "configuration_error": ("CONFIGURATION_ERROR", "语音转写服务未正确配置", False),
        "rate_limited": ("RATE_LIMITED", "语音转写服务繁忙，请稍后重试", True),
        "timeout": ("ASR_TIMEOUT", "语音最终识别超时", True),
    }
    code, message, recoverable = mapping.get(exc.error_type, ("ASR_PROVIDER_ERROR", "上游语音服务暂时不可用", True))
    retry_after_ms = round(exc.retry_after_seconds * 1000) if exc.retry_after_seconds is not None else None
    await send_error(websocket, code, message, recoverable, retry_after_ms)


async def send_error(
    websocket: WebSocket,
    code: str,
    message: str,
    recoverable: bool,
    retry_after_ms: int | None = None,
) -> None:
    payload: dict[str, object] = {"type": "error", "code": code, "message": message, "recoverable": recoverable}
    if retry_after_ms is not None:
        payload["retry_after_ms"] = retry_after_ms
    await websocket.send_json(payload)
