import httpx
import pytest

from lili_voice_input.config import Settings
from lili_voice_input.providers.siliconflow_asr import SiliconFlowAsrProvider


@pytest.mark.asyncio
async def test_siliconflow_provider_sends_multipart_audio() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["content_type"] = request.headers["content-type"]
        body = request.read()
        captured["body"] = body
        return httpx.Response(200, json={"text": "测试转写"})

    settings = Settings(
        ASR_API_KEY="test-key",
        ASR_MODEL="FunAudioLLM/SenseVoiceSmall",
        ASR_BASE_URL="https://api.siliconflow.cn/v1",
        POLISH_ENABLED=False,
    )
    client = httpx.AsyncClient(base_url=settings.asr_base_url, transport=httpx.MockTransport(handler))
    provider = SiliconFlowAsrProvider(settings, client=client)
    try:
        result = await provider.transcribe(b"wav-bytes", audio_format="wav", language="zh")
    finally:
        await client.aclose()

    assert result == "测试转写"
    assert captured["path"] == "/v1/audio/transcriptions"
    assert "multipart/form-data" in str(captured["content_type"])
    body = bytes(captured["body"])
    assert b'name="model"' in body
    assert b"FunAudioLLM/SenseVoiceSmall" in body
    assert b'name="language"' not in body
    assert b"wav-bytes" in body


def test_siliconflow_provider_can_be_selected() -> None:
    settings = Settings(
        ASR_PROVIDER="siliconflow",
        ASR_API_KEY="test-key",
        ASR_MODEL="FunAudioLLM/SenseVoiceSmall",
        POLISH_ENABLED=False,
    )
    assert settings.asr_provider == "siliconflow"
