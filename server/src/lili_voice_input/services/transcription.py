from __future__ import annotations

from lili_voice_input.audio.converter import AudioConverter
from lili_voice_input.audio.pcm import SAMPLE_RATE, SAMPLE_WIDTH_BYTES, wav_to_pcm16
from lili_voice_input.config import Settings
from lili_voice_input.providers.interfaces import AsrProvider
from lili_voice_input.services.polishing import PolishingService
from lili_voice_input.services.streaming import FinalResult, transcribe_pcm


class TranscriptionService:
    def __init__(
        self,
        settings: Settings,
        converter: AudioConverter,
        asr_provider: AsrProvider,
        polishing_service: PolishingService,
    ) -> None:
        self.settings = settings
        self.converter = converter
        self.asr_provider = asr_provider
        self.polishing_service = polishing_service

    async def transcribe_upload(self, content: bytes, *, language: str | None = "zh") -> FinalResult:
        wav_content = await self.converter.convert_to_wav(content)
        pcm = wav_to_pcm16(wav_content)
        if len(pcm) > self.settings.stt_max_duration_seconds * SAMPLE_RATE * SAMPLE_WIDTH_BYTES:
            raise ValueError("max_duration_exceeded")
        return await transcribe_pcm(
            pcm,
            asr_provider=self.asr_provider,
            polish_service=self.polishing_service,
            stream_options=self.settings.stream_options(),
            language=language,
        )

