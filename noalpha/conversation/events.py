from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass
class ChatEvent:
    event_id: str = field(default_factory=lambda: uuid4().hex[:16])
    event_type: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    turn_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        payload = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "turn_id": self.turn_id,
            **self.data,
        }
        data_str = json.dumps(payload, ensure_ascii=False)
        return f"id: {self.event_id}\nevent: {self.event_type}\ndata: {data_str}\n\n"


class EventBus:
    def __init__(self) -> None:
        self._buffers: dict[str, list[ChatEvent]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[ChatEvent]]] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.MAX_BUFFER = 500

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: ChatEvent) -> None:
        with self._lock:
            buf = self._buffers.setdefault(event.session_id, [])
            buf.append(event)
            if len(buf) > self.MAX_BUFFER:
                buf.pop(0)
        if self._loop:
            for sub in list(self._subscribers.get(event.session_id, [])):
                self._loop.call_soon_threadsafe(sub.put_nowait, event)

    async def subscribe(self, session_id: str, last_event_id: str = "") -> Any:
        queue: asyncio.Queue[ChatEvent] = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(session_id, []).append(queue)
        try:
            # Replay buffered events after last_event_id (identity match — ids are random, not ordered)
            with self._lock:
                buf = list(self._buffers.get(session_id, []))
            start = 0
            if last_event_id:
                for i, event in enumerate(buf):
                    if event.event_id == last_event_id:
                        start = i + 1
                        break
            for event in buf[start:]:
                yield event
            # Stream live events
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield event
                except asyncio.TimeoutError:
                    yield ChatEvent(
                        event_type="heartbeat",
                        session_id=session_id,
                        data={"timestamp": time.time()},
                    )
        finally:
            with self._lock:
                subs = self._subscribers.get(session_id, [])
                if queue in subs:
                    subs.remove(queue)

    async def replay(self, session_id: str, last_event_id: str = "") -> list[ChatEvent]:
        with self._lock:
            buf = self._buffers.get(session_id, [])
            return [e for e in buf if not last_event_id or e.event_id > last_event_id]

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._buffers.pop(session_id, None)


# Global singleton
event_bus = EventBus()
