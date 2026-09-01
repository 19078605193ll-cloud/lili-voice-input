from __future__ import annotations

from lili_voice_input.audio.converter import AudioConverter
from lili_voice_input.audio.pcm import SAMPLE_RATE, SAMPLE_WIDTH_BYTES, wav_to_pcm16
from lili_voice_input.config import Settings
from lili_voice_input.providers.interfaces import AsrProvider
from lili_voice_input.services.asr_scheduler import AsrScheduler
from lili_voice_input.services.polishing import PolishingService
from lili_voice_input.services.streaming import FinalResult, transcribe_pcm


class TranscriptionService:
    def __init__(
        self,
        settings: Settings,
        converter: AudioConverter,
        asr_provider: AsrProvider,
        polishing_service: PolishingService,
        asr_scheduler: AsrScheduler | None = None,
    ) -> None:
        self.settings = settings
        self.converter = converter
        self.asr_provider = asr_provider
        self.polishing_service = polishing_service
        self.asr_scheduler = asr_scheduler

    async def transcribe_upload(
        self,
        content: bytes,
        *,
        language: str | None = "zh",
        admission_wait_ms: int = 0,
    ) -> FinalResult:
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
            asr_scheduler=self.asr_scheduler,
            admission_wait_ms=admission_wait_ms,
        )
