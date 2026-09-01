import jwt
import pytest

from lili_voice_input.api.auth import AnonymousTokenService
from lili_voice_input.config import Settings
from lili_voice_input.services.runtime import FixedWindowRateLimiter, RedisRuntime


def settings(**overrides: object) -> Settings:
    return Settings(
        anonymous_tokens_enabled=True,
        anonymous_token_secret="test-secret-that-is-at-least-thirty-two-bytes",
        polish_enabled=False,
        **overrides,
    )


@pytest.mark.asyncio
async def test_anonymous_token_binds_subject_and_origin() -> None:
    config = settings()
    service = AnonymousTokenService(config, FixedWindowRateLimiter(RedisRuntime(config), "test"))

    token = await service.issue("browser-id", "127.0.0.1", "https://example.test")
    claims = jwt.decode(
        token,
        config.anonymous_token_secret,
        algorithms=["HS256"],
        audience="lili-voice-input",
    )

    assert claims["kind"] == "anonymous"
    assert claims["sub"].startswith("anon:")
    assert service.authenticate(token, "https://example.test") is not None
    assert service.authenticate(token, "https://other.test") is None


@pytest.mark.asyncio
async def test_anonymous_token_issue_rate_limit_uses_client_and_ip() -> None:
    config = settings(anonymous_token_issue_limit_per_minute=1)
    service = AnonymousTokenService(config, FixedWindowRateLimiter(RedisRuntime(config), "test"))

    await service.issue("browser-id", "127.0.0.1", "https://example.test")
    with pytest.raises(Exception) as raised:
        await service.issue("browser-id", "127.0.0.1", "https://example.test")
    assert getattr(raised.value, "status_code", None) == 429
