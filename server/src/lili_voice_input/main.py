from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from lili_voice_input.api.auth import AnonymousTokenService
from lili_voice_input.api.health import router as health_router
from lili_voice_input.api.http import router as http_router
from lili_voice_input.api.metrics import router as metrics_router
from lili_voice_input.api.operations import router as operations_router
from lili_voice_input.api.tokens import router as tokens_router
from lili_voice_input.api.websocket import router as websocket_router
from lili_voice_input.audio.converter import AudioConverter
from lili_voice_input.config import get_settings
from lili_voice_input.providers.dashscope_asr import DashScopeAsrProvider
from lili_voice_input.providers.mock import MockAsrProvider, MockTextPolisher
from lili_voice_input.providers.openai_polisher import OpenAICompatiblePolisher
from lili_voice_input.providers.openrouter_asr import OpenRouterAsrProvider
from lili_voice_input.providers.siliconflow_asr import SiliconFlowAsrProvider
from lili_voice_input.services import metrics
from lili_voice_input.services.admission import (
    HybridAdmissionController,
    LocalAdmissionController,
    RedisAdmissionController,
)
from lili_voice_input.services.asr_scheduler import AsrScheduler
from lili_voice_input.services.distributed_limiter import RedisLeaseLimiter
from lili_voice_input.services.draining import ConnectionRegistry
from lili_voice_input.services.limiter import BoundedLimiter
from lili_voice_input.services.polishing import PolishingService
from lili_voice_input.services.runtime import FixedWindowRateLimiter, RedisRuntime
from lili_voice_input.services.transcription import TranscriptionService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    redis_runtime = RedisRuntime(settings)
    await redis_runtime.start()
    asr_provider = (
        MockAsrProvider(settings.mock_provider_delay_ms, settings.mock_asr_text_file)
        if settings.use_mock_asr
        else DashScopeAsrProvider(settings)
        if settings.asr_provider == "dashscope"
        else SiliconFlowAsrProvider(settings)
        if settings.asr_provider == "siliconflow"
        else OpenRouterAsrProvider(settings)
    )
    polish_provider = None
    if settings.polish_enabled:
        polish_provider = (
            MockTextPolisher(settings.mock_provider_delay_ms)
            if settings.use_mock_polish
            else OpenAICompatiblePolisher(settings)
        )
    asr_global_limiter = RedisLeaseLimiter(
        redis_runtime,
        f"{settings.redis_key_prefix}:asr:leases",
        settings.stt_asr_global_concurrency,
        settings.redis_provider_lease_ttl_seconds,
    )
    polish_global_limiter = RedisLeaseLimiter(
        redis_runtime,
        f"{settings.redis_key_prefix}:polish:leases",
        settings.polish_global_concurrency,
        settings.redis_provider_lease_ttl_seconds,
    )
    asr_scheduler = AsrScheduler(
        asr_provider,
        concurrency=settings.stt_max_concurrency,
        queue_size=settings.stt_asr_queue_size,
        queue_timeout_seconds=settings.stt_asr_queue_timeout_seconds,
        request_timeout_seconds=settings.stt_request_timeout_seconds,
        global_limiter=asr_global_limiter,
        session_max_in_flight=settings.stt_segment_max_in_flight,
    )
    await asr_scheduler.start()
    polish_limiter = BoundedLimiter(
        settings.polish_local_concurrency,
        settings.polish_queue_size,
        settings.polish_queue_timeout_seconds,
        inflight_metric=metrics.POLISH_INFLIGHT,
        queue_metric=metrics.POLISH_QUEUE_DEPTH,
        wait_metric=metrics.POLISH_QUEUE_WAIT,
    )
    polishing_service = PolishingService(
        polish_provider,
        enabled=settings.polish_enabled,
        limiter=polish_limiter,
        global_limiter=polish_global_limiter,
        queue_timeout_seconds=settings.polish_queue_timeout_seconds,
    )
    local_admission = LocalAdmissionController(
        settings.stt_max_active_sessions,
        settings.stt_admission_queue_size,
        settings.stt_admission_wait_seconds,
        settings.anonymous_max_active_sessions,
    )
    if settings.redis_enabled:
        distributed_admission = RedisAdmissionController(
            redis_runtime,
            settings.redis_key_prefix,
            settings.stt_global_active_sessions,
            settings.stt_admission_queue_size,
            settings.stt_admission_wait_seconds,
            settings.anonymous_max_active_sessions,
            settings.redis_lease_ttl_seconds,
        )
        admission = HybridAdmissionController(local_admission, distributed_admission)
    else:
        admission = local_admission
    rate_limiter = FixedWindowRateLimiter(redis_runtime, settings.redis_key_prefix)
    app.state.settings = settings
    app.state.redis_runtime = redis_runtime
    app.state.asr_provider = asr_provider
    app.state.asr_scheduler = asr_scheduler
    app.state.polishing_service = polishing_service
    app.state.admission = admission
    app.state.token_service = AnonymousTokenService(settings, rate_limiter)
    app.state.connection_registry = ConnectionRegistry()
    app.state.draining = False
    app.state.transcription_service = TranscriptionService(
        settings,
        AudioConverter(settings),
        asr_provider,
        polishing_service,
        asr_scheduler,
    )
    yield
    app.state.draining = True
    await app.state.connection_registry.close_now()
    await asr_scheduler.close()
    await asr_provider.close()
    if polish_provider is not None:
        await polish_provider.close()
    await redis_runtime.close()


app = FastAPI(
    title="lili-voice-input",
    version="0.1.0",
    description="Self-hosted streaming speech input API",
    lifespan=lifespan,
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Voice-Fallback"],
)
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(tokens_router)
app.include_router(operations_router)
app.include_router(http_router)
app.include_router(websocket_router)


@app.exception_handler(HTTPException)
async def api_http_error(request: Request, exc: HTTPException) -> JSONResponse:
    if not request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    code_by_status = {
        400: "INVALID_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        413: "UPLOAD_TOO_LARGE",
        422: "INVALID_AUDIO",
        429: "CAPACITY_REACHED",
        502: "ASR_PROVIDER_ERROR",
        503: "CONFIGURATION_ERROR",
        504: "ASR_TIMEOUT",
    }
    retry_after_ms: int | None = None
    if exc.headers:
        raw_retry_ms = exc.headers.get("X-Retry-After-Ms")
        raw_retry_seconds = exc.headers.get("Retry-After")
        try:
            retry_after_ms = (
                int(raw_retry_ms)
                if raw_retry_ms
                else round(float(raw_retry_seconds) * 1000)
                if raw_retry_seconds
                else None
            )
        except ValueError:
            retry_after_ms = None
    explicit_code = exc.headers.get("X-Error-Code") if exc.headers else None
    content = {
        "type": "error",
        "code": explicit_code or code_by_status.get(exc.status_code, "REQUEST_FAILED"),
        "message": str(exc.detail),
        "recoverable": exc.status_code in {429, 502, 504}
        or explicit_code in {"CAPACITY_REACHED", "RATE_LIMITED", "QUEUE_TIMEOUT", "SERVER_RESTART"},
    }
    if retry_after_ms is not None:
        content["retry_after_ms"] = retry_after_ms
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content=content,
    )


@app.exception_handler(RequestValidationError)
async def api_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    if not request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    return JSONResponse(
        status_code=422,
        content={
            "type": "error",
            "code": "INVALID_REQUEST",
            "message": "请求参数无效",
            "recoverable": False,
        },
    )


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/demo/")


static_root = settings.static_root or Path(__file__).resolve().parent / "static"
demo_root = static_root / "demo"
sdk_root = static_root / "sdk"
if demo_root.exists():
    app.mount("/demo", StaticFiles(directory=demo_root, html=True), name="demo")
else:

    @app.get("/demo/", response_class=HTMLResponse, include_in_schema=False)
    async def demo_unbuilt() -> str:
        return "<main><h1>Demo not built</h1><p>Run <code>npm run dev</code> for development or <code>npm run build</code>.</p></main>"


if sdk_root.exists():
    app.mount("/sdk", StaticFiles(directory=sdk_root), name="sdk")
