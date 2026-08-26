from __future__ import annotations

from dataclasses import dataclass

from lili_voice_input.audio.pcm import (
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    bytes_to_milliseconds,
    milliseconds_to_bytes,
    pcm_rms,
)


@dataclass(frozen=True, slots=True)
class AudioSegment:
    sequence: int
    pcm: bytes
    audio_end_ms: int

    @property
    def duration_ms(self) -> int:
        return bytes_to_milliseconds(len(self.pcm))


class AudioSegmenter:
    """Cut after target duration at silence, or force-cut at the maximum."""

    def __init__(
        self,
        *,
        target_seconds: int,
        max_seconds: int,
        overlap_ms: int,
        silence_ms: int,
    ) -> None:
        if target_seconds <= 0 or max_seconds < target_seconds:
            raise ValueError("invalid_segment_duration")
        if overlap_ms < 0 or silence_ms <= 0 or overlap_ms >= target_seconds * 1000:
            raise ValueError("invalid_segment_boundary")
        self.target_bytes = target_seconds * SAMPLE_RATE * SAMPLE_WIDTH_BYTES
        self.max_bytes = max_seconds * SAMPLE_RATE * SAMPLE_WIDTH_BYTES
        self.overlap_bytes = milliseconds_to_bytes(overlap_ms)
        self.silence_ms = silence_ms
        self._buffer = bytearray()
        self._new_bytes = 0
        self._total_bytes = 0
        self._consecutive_silence_ms = 0
        self._noise_floor = 0.004
        self._has_speech = False
        self._has_new_speech = False
        self._sequence = 0

    @property
    def has_speech(self) -> bool:
        return self._has_speech

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def add_frame(self, pcm: bytes) -> AudioSegment | None:
        if not pcm or len(pcm) % SAMPLE_WIDTH_BYTES:
            raise ValueError("invalid_pcm16_frame")
        self._buffer.extend(pcm)
        self._new_bytes += len(pcm)
        self._total_bytes += len(pcm)
        energy = pcm_rms(pcm)
        threshold = max(0.012, self._noise_floor * 2.8)
        frame_ms = bytes_to_milliseconds(len(pcm))
        if energy >= threshold:
            self._has_speech = True
            self._has_new_speech = True
            self._consecutive_silence_ms = 0
        else:
            self._noise_floor = self._noise_floor * 0.92 + energy * 0.08
            self._consecutive_silence_ms += frame_ms
        at_silence = self._new_bytes >= self.target_bytes and self._consecutive_silence_ms >= self.silence_ms
        if self._new_bytes >= self.max_bytes or at_silence:
            return self._cut()
        return None

    def finalize(self) -> AudioSegment | None:
        if not self._has_new_speech:
            self._clear_buffer()
            return None
        return self._cut(retain_overlap=False)

    def close(self) -> None:
        self._clear_buffer()

    def _cut(self, *, retain_overlap: bool = True) -> AudioSegment | None:
        if not self._has_new_speech:
            self._clear_buffer()
            return None
        pcm = bytes(self._buffer)
        segment = AudioSegment(self._sequence, pcm, bytes_to_milliseconds(self._total_bytes))
        self._sequence += 1
        overlap = pcm[-self.overlap_bytes :] if retain_overlap and self.overlap_bytes else b""
        self._buffer = bytearray(overlap)
        self._new_bytes = 0
        self._consecutive_silence_ms = 0
        self._has_new_speech = False
        return segment

    def _clear_buffer(self) -> None:
        self._buffer.clear()
        self._new_bytes = 0
        self._consecutive_silence_ms = 0
        self._has_new_speech = False
