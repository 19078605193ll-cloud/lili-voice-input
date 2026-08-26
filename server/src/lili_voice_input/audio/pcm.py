from __future__ import annotations

from io import BytesIO
import math
import wave

SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2
FRAME_DURATION_MS = 100


def milliseconds_to_bytes(duration_ms: int) -> int:
    return round(SAMPLE_RATE * SAMPLE_WIDTH_BYTES * duration_ms / 1000)


def bytes_to_milliseconds(length: int) -> int:
    return round(length / (SAMPLE_RATE * SAMPLE_WIDTH_BYTES) * 1000)


def pcm16_to_wav(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    if not pcm or len(pcm) % SAMPLE_WIDTH_BYTES:
        raise ValueError("PCM16 data must be non-empty and aligned")
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


def wav_to_pcm16(wav_content: bytes) -> bytes:
    try:
        with wave.open(BytesIO(wav_content), "rb") as wav_file:
            if (
                wav_file.getnchannels() != 1
                or wav_file.getsampwidth() != SAMPLE_WIDTH_BYTES
                or wav_file.getframerate() != SAMPLE_RATE
                or wav_file.getcomptype() != "NONE"
            ):
                raise ValueError("unsupported_wav")
            pcm = wav_file.readframes(wav_file.getnframes())
    except (EOFError, wave.Error) as exc:
        raise ValueError("invalid_wav") from exc
    if not pcm or len(pcm) % SAMPLE_WIDTH_BYTES:
        raise ValueError("invalid_wav")
    return pcm


def pcm_rms(pcm: bytes) -> float:
    if not pcm or len(pcm) % SAMPLE_WIDTH_BYTES:
        return 0.0
    samples = memoryview(pcm).cast("h")
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return math.sqrt(mean_square) / 32768.0

