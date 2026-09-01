from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
import uuid
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request, status

from lili_voice_input.config import Settings
from lili_voice_input.services.runtime import FixedWindowRateLimiter, RuntimeUnavailable


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    kind: str
    trusted: bool = False


def token_matches(settings: Settings, supplied: str | None) -> bool:
    configured = settings.service_token
    if not configured:
        return not settings.anonymous_tokens_enabled
    return bool(supplied) and secrets.compare_digest(configured, supplied)


def client_ip(scope_client: tuple[str, int] | None, headers: object, settings: Settings) -> str:
    direct = scope_client[0] if scope_client else "unknown"
    forwarded = getattr(headers, "get", lambda _name, _default=None: None)("x-forwarded-for")
    if not forwarded or not _trusted_proxy(direct, settings.trusted_proxy_cidrs):
        return direct
    return forwarded.split(",", 1)[0].strip() or direct


def _trusted_proxy(address: str, cidrs: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)
    except ValueError:
        return False


class AnonymousTokenService:
    def __init__(self, settings: Settings, limiter: FixedWindowRateLimiter) -> None:
        self.settings = settings
        self.limiter = limiter

    def _digest(self, value: str) -> str:
        return hmac.new(
            self.settings.anonymous_token_secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    async def issue(self, client_id: str, ip: str, origin: str) -> str:
        if not self.settings.anonymous_tokens_enabled:
            raise HTTPException(status_code=404, detail="匿名令牌未启用")
        limit = self.settings.anonymous_token_issue_limit_per_minute
        ip_key = self._digest(f"ip:{ip}")
        client_key = self._digest(f"client:{client_id}")
        try:
            allowed_ip = await self.limiter.allow(f"token-ip:{ip_key}", limit)
            allowed_client = await self.limiter.allow(f"token-client:{client_key}", limit)
        except RuntimeUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail="容量控制服务不可用",
                headers={"Retry-After": "5", "X-Retry-After-Ms": "5000", "X-Error-Code": "CAPACITY_REACHED"},
            ) from exc
        if not allowed_ip or not allowed_client:
            raise HTTPException(
                status_code=429,
                detail="匿名令牌申请过于频繁",
                headers={"Retry-After": "60", "X-Retry-After-Ms": "60000", "X-Error-Code": "RATE_LIMITED"},
            )
        now = int(time.time())
        payload = {
            "sub": f"anon:{client_key}",
            "origin": origin,
            "iat": now,
            "exp": now + self.settings.anonymous_token_ttl_seconds,
            "jti": uuid.uuid4().hex,
            "kind": "anonymous",
            "aud": "lili-voice-input",
        }
        return jwt.encode(payload, self.settings.anonymous_token_secret, algorithm="HS256")

    def authenticate(self, supplied: str | None, origin: str | None) -> Principal | None:
        if supplied and self.settings.service_token and secrets.compare_digest(self.settings.service_token, supplied):
            return Principal("service", "service", True)
        if not self.settings.anonymous_tokens_enabled:
            return Principal("legacy", "legacy", True) if not self.settings.service_token else None
        if not supplied:
            return None
        try:
            claims = jwt.decode(
                supplied,
                self.settings.anonymous_token_secret,
                algorithms=["HS256"],
                audience="lili-voice-input",
                options={"require": ["sub", "origin", "iat", "exp", "jti", "kind"]},
            )
        except jwt.PyJWTError:
            return None
        if claims.get("kind") != "anonymous" or not str(claims.get("sub", "")).startswith("anon:"):
            return None
        token_origin = str(claims.get("origin") or "")
        if token_origin.rstrip("/") != (origin or "").rstrip("/"):
            return None
        return Principal(str(claims["sub"]), "anonymous")

    async def allow_session_start(self, principal: Principal, ip: str) -> bool:
        if principal.trusted:
            return True
        limit = self.settings.anonymous_session_start_limit_per_minute
        ip_key = self._digest(f"ip:{ip}")
        allowed_subject = await self.limiter.allow(f"session-sub:{principal.subject}", limit)
        allowed_ip = await self.limiter.allow(f"session-ip:{ip_key}", limit * 5)
        return allowed_subject and allowed_ip


async def require_http_token(request: Request) -> Principal:
    settings: Settings = request.app.state.settings
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    service: AnonymousTokenService = request.app.state.token_service
    principal = service.authenticate(supplied, request.headers.get("origin"))
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="访问令牌无效")
    ip = client_ip(request.client, request.headers, settings)
    try:
        allowed = await service.allow_session_start(principal, ip)
    except RuntimeUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="容量控制服务不可用",
            headers={"Retry-After": "5", "X-Retry-After-Ms": "5000", "X-Error-Code": "CAPACITY_REACHED"},
        ) from exc
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="语音请求过于频繁，请稍后重试",
            headers={"Retry-After": "60", "X-Retry-After-Ms": "60000", "X-Error-Code": "RATE_LIMITED"},
        )
    request.state.principal = principal
    return principal
