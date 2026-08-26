from __future__ import annotations

import shutil

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from lili_voice_input import __version__
from lili_voice_input.api.schemas import HealthResponse
from lili_voice_input.config import Settings

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get("/health/ready", response_model=HealthResponse)
async def ready(request: Request) -> HealthResponse | JSONResponse:
    settings: Settings = request.app.state.settings
    errors = settings.readiness_errors()
    if shutil.which(settings.ffmpeg_binary) is None:
        errors.append(f"FFmpeg binary not found: {settings.ffmpeg_binary}")
    payload = HealthResponse(status="not_ready" if errors else "ok", version=__version__, errors=errors)
    if errors:
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload.model_dump())
    return payload

