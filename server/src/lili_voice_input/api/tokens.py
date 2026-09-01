from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from lili_voice_input.api.auth import AnonymousTokenService, client_ip
from lili_voice_input.api.schemas import AnonymousTokenRequest, AnonymousTokenResponse
from lili_voice_input.config import Settings

router = APIRouter(prefix="/v1", tags=["authentication"])


@router.post("/anonymous-tokens", response_model=AnonymousTokenResponse)
async def create_anonymous_token(payload: AnonymousTokenRequest, request: Request) -> AnonymousTokenResponse:
    settings: Settings = request.app.state.settings
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin required")
    if "*" not in settings.allowed_origins and origin not in settings.allowed_origins:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin not allowed")
    service: AnonymousTokenService = request.app.state.token_service
    token = await service.issue(payload.client_id, client_ip(request.client, request.headers, settings), origin)
    return AnonymousTokenResponse(token=token, expires_in_seconds=settings.anonymous_token_ttl_seconds)
