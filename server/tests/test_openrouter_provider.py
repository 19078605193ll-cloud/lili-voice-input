import base64

import httpx
import pytest

from lili_voice_input.config import Settings
from lili_voice_input.providers.openrouter_asr import OpenRouterAsrProvider


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

