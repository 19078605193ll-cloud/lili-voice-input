from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from lili_voice_input.api.health import router as health_router
from lili_voice_input.api.http import router as http_router
from lili_voice_input.api.websocket import router as websocket_router
from lili_voice_input.audio.converter import AudioConverter
from lili_voice_input.config import get_settings
from lili_voice_input.providers.openai_polisher import OpenAICompatiblePolisher
from lili_voice_input.providers.openrouter_asr import OpenRouterAsrProvider
from lili_voice_input.services.capacity import SessionCapacity
from lili_voice_input.services.polishing import PolishingService
from lili_voice_input.services.transcription import TranscriptionService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    asr_provider = OpenRouterAsrProvider(settings)
    polish_provider = OpenAICompatiblePolisher(settings) if settings.polish_enabled else None
    polishing_service = PolishingService(polish_provider, enabled=settings.polish_enabled)
    app.state.settings = settings
    app.state.asr_provider = asr_provider
    app.state.polishing_service = polishing_service
    app.state.session_capacity = SessionCapacity(settings.stt_max_active_sessions)
    app.state.transcription_service = TranscriptionService(
        settings,
        AudioConverter(settings),
        asr_provider,
        polishing_service,
    )
    yield
    await asr_provider.close()
    if polish_provider is not None:
        await polish_provider.close()


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
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(health_router)
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
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "type": "error",
            "code": code_by_status.get(exc.status_code, "REQUEST_FAILED"),
            "message": str(exc.detail),
            "recoverable": exc.status_code in {429, 502, 504},
        },
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
