from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request, status

from lili_voice_input.config import Settings

router = APIRouter(prefix="/internal", tags=["operations"])


@router.post("/drain")
async def begin_drain(request: Request) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not settings.service_token or not secrets.compare_digest(settings.service_token, supplied):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service token")
    request.app.state.draining = True
    request.app.state.connection_registry.schedule_close(30)
    return {"status": "draining", "force_close_after_seconds": 30}
