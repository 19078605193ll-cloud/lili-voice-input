import asyncio
import threading
import time
from array import array
from dataclasses import dataclass

import pytest

from lili_voice_input.audio.converter import AudioConverter
from lili_voice_input.audio.merger import merge_transcript_segments, merge_transcripts
from lili_voice_input.audio.pcm import SAMPLE_RATE, pcm16_to_wav, wav_to_pcm16
from lili_voice_input.audio.segmenter import AudioSegmenter
from lili_voice_input.config import Settings


def pcm(value: int, milliseconds: int) -> bytes:
    return array("h", [value] * (SAMPLE_RATE * milliseconds // 1000)).tobytes()


def test_pcm_wav_round_trip() -> None:
    source = pcm(1200, 100)
    assert wav_to_pcm16(pcm16_to_wav(source)) == source


def test_pcm_requires_aligned_non_empty_input() -> None:
    with pytest.raises(ValueError):
        pcm16_to_wav(b"")
    with pytest.raises(ValueError):
        pcm16_to_wav(b"x")


def test_segmenter_cuts_at_silence_after_target() -> None:
    segmenter = AudioSegmenter(target_seconds=1, max_seconds=2, overlap_ms=100, silence_ms=200)
    segment = None
    for _ in range(10):
        segment = segmenter.add_frame(pcm(5000, 100)) or segment
    for _ in range(2):
        segment = segmenter.add_frame(pcm(0, 100)) or segment
    assert segment is not None
    assert segment.duration_ms == 1200
    assert segmenter.buffered_bytes == SAMPLE_RATE * 2 // 10


def test_segmenter_discards_all_silence() -> None:
    segmenter = AudioSegmenter(target_seconds=1, max_seconds=2, overlap_ms=100, silence_ms=200)
    segments = [segmenter.add_frame(pcm(0, 100)) for _ in range(25)]

    assert all(segment is None for segment in segments)
    assert segmenter.finalize() is None
    assert segmenter.buffered_bytes == 0
    assert not segmenter.has_speech


def test_segmenter_rejects_a_single_transient_noise_frame() -> None:
    segmenter = AudioSegmenter(
        target_seconds=1,
        max_seconds=2,
        overlap_ms=100,
        silence_ms=200,
        min_speech_ms=200,
    )

    assert segmenter.add_frame(pcm(5000, 100)) is None
    assert segmenter.add_frame(pcm(0, 200)) is None
    assert segmenter.finalize() is None
    assert not segmenter.has_speech


def test_segmenter_accepts_minimum_consecutive_speech() -> None:
    segmenter = AudioSegmenter(
        target_seconds=1,
        max_seconds=2,
        overlap_ms=100,
        silence_ms=200,
        min_speech_ms=200,
    )

    assert segmenter.add_frame(pcm(5000, 100)) is None
    assert segmenter.add_frame(pcm(5000, 100)) is None
    segment = segmenter.finalize()

    assert segment is not None
    assert segment.duration_ms == 200
    assert segmenter.has_speech


def test_segmenter_does_not_emit_silence_after_speech() -> None:
    segmenter = AudioSegmenter(target_seconds=1, max_seconds=2, overlap_ms=100, silence_ms=200)
    segments = []
    for _ in range(10):
        segment = segmenter.add_frame(pcm(5000, 100))
        if segment is not None:
            segments.append(segment)
    for _ in range(22):
        segment = segmenter.add_frame(pcm(0, 100))
        if segment is not None:
            segments.append(segment)

    assert [segment.sequence for segment in segments] == [0]
    assert segmenter.finalize() is None
    assert segmenter.has_speech


def test_segmenter_force_cuts_continuous_speech_with_contiguous_sequences() -> None:
    segmenter = AudioSegmenter(target_seconds=1, max_seconds=2, overlap_ms=100, silence_ms=200)
    segments = []
    for _ in range(40):
        segment = segmenter.add_frame(pcm(5000, 100))
        if segment is not None:
            segments.append(segment)

    assert [segment.sequence for segment in segments] == [0, 1]


def test_merge_removes_exact_and_fuzzy_boundaries() -> None:
    assert merge_transcripts("修改配置后需要重启", "需要重启后端") == "修改配置后需要重启后端"
    assert merge_transcript_segments(["hello world", "world again"]) == "hello world again"


@dataclass
class _CompletedProcess:
    returncode: int = 0
    stdout: bytes = b"RIFF"
    stderr: bytes = b""


@pytest.mark.asyncio
async def test_ffmpeg_converter_never_exceeds_configured_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    maximum = 0
    lock = threading.Lock()

    def fake_run(*args: object, **kwargs: object) -> _CompletedProcess:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return _CompletedProcess()

    monkeypatch.setattr("lili_voice_input.audio.converter.subprocess.run", fake_run)
    converter = AudioConverter(
        Settings(polish_enabled=False, ffmpeg_max_concurrency=2, ffmpeg_queue_size=8, ffmpeg_queue_timeout_seconds=1)
    )
    await asyncio.gather(*(converter.convert_to_wav(b"audio") for _ in range(8)))
    assert maximum == 2
