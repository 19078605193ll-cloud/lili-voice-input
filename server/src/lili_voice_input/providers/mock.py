from __future__ import annotations

import asyncio
from pathlib import Path


class MockAsrProvider:
    def __init__(self, delay_ms: int = 10, text_file: Path | None = None) -> None:
        self.delay_seconds = delay_ms / 1000
        self.text = text_file.read_text(encoding="utf-8").strip() if text_file is not None else "模拟语音转写结果"

    async def transcribe(self, audio: bytes, *, audio_format: str, language: str | None = None) -> str:
        await asyncio.sleep(self.delay_seconds)
        return self.text

    async def close(self) -> None:
        return None


class MockTextPolisher:
    def __init__(self, delay_ms: int = 10) -> None:
        self.delay_seconds = delay_ms / 1000

    async def polish(self, transcript: str) -> str:
        await asyncio.sleep(self.delay_seconds)
        return transcript.strip()

    async def close(self) -> None:
        return None
