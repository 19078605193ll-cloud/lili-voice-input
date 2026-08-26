from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from lili_voice_input.config import Settings


def token_matches(settings: Settings, supplied: str | None) -> bool:
    configured = settings.service_token
    if not configured:
        return True
    return bool(supplied) and secrets.compare_digest(configured, supplied)


async def require_http_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    if not token_matches(settings, supplied):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="访问令牌无效")

