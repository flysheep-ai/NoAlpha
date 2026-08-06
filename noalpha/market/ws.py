from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from noalpha.market.gateway import market_gateway

logger = logging.getLogger(__name__)


class MarketWebSocketManager:
    MAX_SYMBOLS_PER_MESSAGE = 50
    MAX_QUEUE_SIZE = 200

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._subscriptions: dict[str, set[str]] = {}  # ws_id -> {symbols}
        self._queues: dict[str, asyncio.Queue] = {}  # ws_id -> queue

    async def handle(self, websocket: WebSocket, ws_id: str) -> None:
        await websocket.accept()
        self._connections[ws_id] = websocket
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self._queues[ws_id] = queue

        # Send initial snapshot
        all_quotes = market_gateway.get_all_latest()
        if all_quotes:
            await websocket.send_json({"type": "snapshot", "quotes": all_quotes})

        try:
            while True:
                # Wait for data from either the subscriber queue or client message
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=3.0)
                    await websocket.send_json({"type": "quote_update", "quote": data})
                except asyncio.TimeoutError:
                    # Send heartbeat
                    try:
                        await websocket.send_json({"type": "heartbeat", "ts": asyncio.get_event_loop().time()})
                    except Exception:
                        break

                # Check for client messages (non-blocking)
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
                    msg = json.loads(raw)
                    action = msg.get("action", "")
                    symbols = self._clean_symbols(msg.get("symbols", []))

                    if action == "subscribe" and symbols:
                        self._subscriptions.setdefault(ws_id, set()).update(symbols)
                        market_gateway.subscribe(symbols, queue)
                        await websocket.send_json({"type": "subscribed", "symbols": symbols})
                    elif action == "unsubscribe":
                        subs = self._subscriptions.get(ws_id, set())
                        subs.difference_update(symbols)
                except asyncio.TimeoutError:
                    pass
                except (json.JSONDecodeError, ValueError) as exc:
                    await websocket.send_json({"type": "error", "error": str(exc)})

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("WS error for %s: %s", ws_id, exc)
        finally:
            market_gateway.unsubscribe_queue(queue)
            self._connections.pop(ws_id, None)
            self._queues.pop(ws_id, None)
            self._subscriptions.pop(ws_id, None)

    def _clean_symbols(self, raw: Any) -> list[str]:
        if not isinstance(raw, list):
            raise ValueError("symbols must be a list")
        if len(raw) > self.MAX_SYMBOLS_PER_MESSAGE:
            raise ValueError(f"too many symbols; max {self.MAX_SYMBOLS_PER_MESSAGE}")
        symbols: list[str] = []
        for item in raw:
            text = str(item).strip().upper()
            if not text or len(text) > 24:
                raise ValueError("invalid symbol")
            symbols.append(text)
        return symbols


ws_manager = MarketWebSocketManager()


def register_market_ws(app: FastAPI) -> None:
    @app.websocket("/api/v1/market/stream")
    async def market_stream(websocket: WebSocket) -> None:
        import uuid
        ws_id = str(uuid.uuid4())[:12]
        await ws_manager.handle(websocket, ws_id)
