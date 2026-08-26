from __future__ import annotations

from typing import Protocol


class AsrProvider(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        *,
        audio_format: str,
        language: str | None = None,
    ) -> str: ...

    async def close(self) -> None: ...


class TextPolisher(Protocol):
    async def polish(self, transcript: str) -> str: ...

    async def close(self) -> None: ...
