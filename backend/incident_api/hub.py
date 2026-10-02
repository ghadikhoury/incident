"""Pushes live updates to every connected dashboard over WebSockets."""

import asyncio
import json
import logging

from fastapi import WebSocket

log = logging.getLogger(__name__)
SEND_TIMEOUT_S = 2.0


class Hub:
    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def send(self, websocket: WebSocket, message: dict) -> None:
        await websocket.send_text(json.dumps(message))

    async def broadcast(self, message: dict) -> None:
        """Send to every client; drop clients that are gone or too slow to keep up."""
        data = json.dumps(message)
        clients = list(self._clients)
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_text(data), SEND_TIMEOUT_S) for ws in clients),
            return_exceptions=True,
        )
        for ws, result in zip(clients, results, strict=True):
            if isinstance(result, Exception):
                log.info("dropping websocket client: %r", result)
                self.disconnect(ws)
