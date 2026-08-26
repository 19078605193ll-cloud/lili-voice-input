from array import array

import pytest

from lili_voice_input.audio.pcm import SAMPLE_RATE
from lili_voice_input.providers.openai_polisher import PolishProviderError
from lili_voice_input.providers.openrouter_asr import AsrProviderError
from lili_voice_input.services.polishing import PolishingService
from lili_voice_input.services.streaming import StreamingSession


class FakeAsr:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = outputs
        self.calls = 0

    async def transcribe(self, audio: bytes, *, audio_format: str, language: str | None = None) -> str:
        output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        if isinstance(output, Exception):
            raise output
        return output

    async def close(self) -> None:
        return None


class FakePolisher:
    def __init__(self, output: str | Exception) -> None:
        self.output = output
        self.calls: list[str] = []

    async def polish(self, transcript: str) -> str:
        self.calls.append(transcript)
        if isinstance(self.output, Exception):
            raise self.output
        return self.output

    async def close(self) -> None:
        return None


def tone(milliseconds: int) -> bytes:
    return array("h", [5000] * (SAMPLE_RATE * milliseconds // 1000)).tobytes()


def silence(milliseconds: int) -> bytes:
    return array("h", [0] * (SAMPLE_RATE * milliseconds // 1000)).tobytes()


@pytest.mark.asyncio
async def test_session_returns_final_without_partial() -> None:
    asr = FakeAsr(["等待八秒"])
    session = StreamingSession(
        asr_provider=asr,
        polish_service=PolishingService(None, enabled=False),
        segment_target_seconds=1,
        segment_max_seconds=2,
        segment_overlap_ms=100,
        segment_silence_ms=200,
        segment_max_in_flight=1,
        segment_max_retries=0,
        request_timeout_seconds=1,
        finalization_timeout_seconds=2,
        max_duration_seconds=10,
    )
    await session.add_audio(tone(200))
    result = await session.finalize()
    assert result.text == "等待八秒"
    assert result.polish_status == "disabled"
    assert result.polish_reason is None
    assert not result.degraded
    assert result.degraded_stage is None
    assert result.segment_count == 1


@pytest.mark.asyncio
async def test_session_applies_plain_text_polish() -> None:
    polisher = FakePolisher("  等待 8 秒。  ")
    session = StreamingSession(
        asr_provider=FakeAsr(["等待八秒"]),
        polish_service=PolishingService(polisher, enabled=True),
        segment_target_seconds=1,
        segment_max_seconds=2,
        segment_overlap_ms=100,
        segment_silence_ms=200,
        segment_max_in_flight=1,
        segment_max_retries=0,
        request_timeout_seconds=1,
        finalization_timeout_seconds=2,
        max_duration_seconds=10,
    )
    await session.add_audio(tone(200))

    result = await session.finalize()

    assert result.text == "等待 8 秒。"
    assert result.polished
    assert result.polish_status == "applied"
    assert result.polish_reason is None
    assert not result.degraded
    assert result.degraded_stage is None
    assert polisher.calls == ["等待八秒"]


@pytest.mark.asyncio
async def test_polish_failure_degrades_to_original_asr() -> None:
    session = StreamingSession(
        asr_provider=FakeAsr(["原始转写"]),
        polish_service=PolishingService(FakePolisher(PolishProviderError("network_error")), enabled=True),
        segment_target_seconds=1,
        segment_max_seconds=2,
        segment_overlap_ms=100,
        segment_silence_ms=200,
        segment_max_in_flight=1,
        segment_max_retries=0,
        request_timeout_seconds=1,
        finalization_timeout_seconds=2,
        max_duration_seconds=10,
    )
    await session.add_audio(tone(200))

    result = await session.finalize()

    assert result.text == "原始转写"
    assert not result.polished
    assert result.polish_status == "fallback"
    assert result.polish_reason == "network_error"
    assert result.degraded
    assert result.degraded_stage == "polish"


@pytest.mark.asyncio
async def test_asr_partial_failure_has_priority_over_polish_failure() -> None:
    session = StreamingSession(
        asr_provider=FakeAsr([AsrProviderError("provider_error"), "成功片段"]),
        polish_service=PolishingService(FakePolisher(PolishProviderError("rate_limited")), enabled=True),
        segment_target_seconds=1,
        segment_max_seconds=2,
        segment_overlap_ms=0,
        segment_silence_ms=200,
        segment_max_in_flight=1,
        segment_max_retries=0,
        request_timeout_seconds=1,
        finalization_timeout_seconds=3,
        max_duration_seconds=10,
    )
    for _ in range(40):
        await session.add_audio(tone(100))

    result = await session.finalize()

    assert result.text == "成功片段"
    assert result.polish_status == "fallback"
    assert result.polish_reason == "rate_limited"
    assert result.degraded
    assert result.degraded_stage == "asr"
    assert result.segment_count == 2
    assert result.failed_segment_count == 1


@pytest.mark.asyncio
async def test_retryable_provider_error_is_retried() -> None:
    asr = FakeAsr([AsrProviderError("provider_error", 500), "成功"])
    session = StreamingSession(
        asr_provider=asr,
        polish_service=PolishingService(None, enabled=False),
        segment_target_seconds=1,
        segment_max_seconds=2,
        segment_overlap_ms=0,
        segment_silence_ms=200,
        segment_max_in_flight=1,
        segment_max_retries=1,
        request_timeout_seconds=1,
        finalization_timeout_seconds=3,
        max_duration_seconds=10,
    )
    await session.add_audio(tone(200))
    result = await session.finalize()
    assert result.text == "成功"
    assert asr.calls == 2


@pytest.mark.asyncio
async def test_silence_after_speech_does_not_submit_extra_asr_segments() -> None:
    asr = FakeAsr(["有效语音"])
    session = StreamingSession(
        asr_provider=asr,
        polish_service=PolishingService(None, enabled=False),
        segment_target_seconds=1,
        segment_max_seconds=2,
        segment_overlap_ms=100,
        segment_silence_ms=200,
        segment_max_in_flight=1,
        segment_max_retries=0,
        request_timeout_seconds=1,
        finalization_timeout_seconds=2,
        max_duration_seconds=10,
    )
    await session.add_audio(tone(1000))
    await session.add_audio(silence(2200))

    assert session.segment_count == 1
    result = await session.finalize()
    assert result.segment_count == 1
    assert asr.calls == 1


@pytest.mark.asyncio
async def test_max_duration_is_enforced_before_buffering() -> None:
    session = StreamingSession(
        asr_provider=FakeAsr(["unused"]),
        polish_service=PolishingService(None, enabled=False),
        max_duration_seconds=1,
    )
    with pytest.raises(ValueError, match="max_duration_exceeded"):
        await session.add_audio(tone(1100))
    await session.close()
