from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionRegistry:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._drain_task: asyncio.Task[None] | None = None

    async def add(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.add(websocket)

    async def remove(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    def schedule_close(self, delay_seconds: float) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._close_after(delay_seconds), name="connection-drain")

    async def close_now(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            await asyncio.gather(self._drain_task, return_exceptions=True)
        await self._close_connections()

    async def _close_after(self, delay_seconds: float) -> None:
        await asyncio.sleep(delay_seconds)
        await self._close_connections()

    async def _close_connections(self) -> None:
        async with self._lock:
            connections = list(self._connections)
        for websocket in connections:
            try:
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "SERVER_RESTART",
                        "message": "服务正在更新，请重新连接",
                        "recoverable": True,
                        "retry_after_ms": 1000,
                    }
                )
                await websocket.close(code=1012, reason="server restart")
            except RuntimeError:
                pass
        if connections:
            logger.info("DRAIN event=connections_closed count=%s", len(connections))
