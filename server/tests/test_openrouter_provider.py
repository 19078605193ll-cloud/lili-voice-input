import base64

import httpx
import pytest

from lili_voice_input.config import Settings
from lili_voice_input.providers.openrouter_asr import (
    AsrProviderError,
    OpenRouterAsrProvider,
    classify_transport_error,
    parse_retry_after,
)


@pytest.mark.asyncio
async def test_openrouter_provider_sends_json_base64_audio() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"text": "测试文本"})

    settings = Settings(ASR_API_KEY="test", ASR_MODEL="test-model", POLISH_ENABLED=False)
    client = httpx.AsyncClient(base_url="https://provider.example", transport=httpx.MockTransport(handler))
    provider = OpenRouterAsrProvider(settings, client=client)
    try:
        result = await provider.transcribe(b"wav-bytes", audio_format="wav", language="zh")
    finally:
        await client.aclose()
    assert result == "测试文本"
    assert captured["path"] == "/audio/transcriptions"
    assert base64.b64encode(b"wav-bytes").decode("ascii") in str(captured["body"])


def test_retry_after_supports_seconds_and_http_dates() -> None:
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("not-a-date") is None
    assert parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") is not None


@pytest.mark.asyncio
async def test_provider_error_exposes_only_safe_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "Retry-After": "2",
                "X-Request-ID": "request-123",
                "X-RateLimit-Remaining": "0",
            },
            json={"error": {"code": "provider_rate_limit", "message": "sensitive provider message"}},
        )

    settings = Settings(ASR_API_KEY="test-secret", ASR_MODEL="test-model", POLISH_ENABLED=False)
    client = httpx.AsyncClient(base_url="https://provider.example", transport=httpx.MockTransport(handler))
    provider = OpenRouterAsrProvider(settings, client=client)
    try:
        with caplog.at_level("WARNING"):
            with pytest.raises(AsrProviderError) as raised:
                await provider.transcribe(b"wav-bytes", audio_format="wav")
    finally:
        await client.aclose()

    error = raised.value
    assert error.error_type == "rate_limited"
    assert error.status_code == 429
    assert error.provider_code == "provider_rate_limit"
    assert error.request_id == "request-123"
    assert error.retry_after_seconds == 2
    assert "test-secret" not in caplog.text
    assert "sensitive provider message" not in caplog.text


def test_transport_error_classification_is_safe_and_specific() -> None:
    request = httpx.Request("POST", "https://provider.example/audio/transcriptions")
    try:
        try:
            raise OSError(-3, "Temporary failure in name resolution for secret-provider.example")
        except OSError as cause:
            raise httpx.ConnectError("connection failed for secret-provider.example", request=request) from cause
    except httpx.ConnectError as exc:
        reason, os_error_number, cause_type = classify_transport_error(exc)

    assert reason == "dns_resolution"
    assert os_error_number == -3
    assert cause_type == "OSError"
