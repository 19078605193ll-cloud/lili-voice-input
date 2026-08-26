from __future__ import annotations

import asyncio


class SessionCapacity:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.active = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            if self.active >= self.maximum:
                return False
            self.active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self.active = max(0, self.active - 1)

