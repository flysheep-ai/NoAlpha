from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from noalpha.market.eastmoney_provider import EastmoneyProvider
from noalpha.market.instruments import (
    CN_INDEX_SECIDS,
    US_INDEX_SECIDS,
    guess_secid_for_symbol,
)
from noalpha.market.live_quote import LiveQuote

logger = logging.getLogger(__name__)

INDEX_SECIDS_DEFAULT = CN_INDEX_SECIDS + US_INDEX_SECIDS


class MarketDataGateway:
    """Polls Eastmoney for CN+US quotes keyed by secid and fans out via
    WebSocket queues. Keeps a rolling intraday history per secid so anomaly
    signals (10m return, volume rate) are computed from real observations."""

    def __init__(self, poll_interval: float = 3.0) -> None:
        self.provider = EastmoneyProvider()
        self.poll_interval = poll_interval
        self._subscriptions: dict[str, set[asyncio.Queue]] = {}  # secid -> queues
        self._latest: dict[str, LiveQuote] = {}  # secid -> quote
        self._latest_by_symbol: dict[str, LiveQuote] = {}
        self._history: dict[str, deque] = {}  # secid -> deque[(epoch, price, volume)]
        self._last_sent_key: dict[str, tuple] = {}
        self._active: set[str] = set(INDEX_SECIDS_DEFAULT)
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._symbol_to_secid: dict[str, str] = {}

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── resolution ──

    def resolve_secid(self, symbol_or_secid: str) -> str | None:
        s = symbol_or_secid.strip()
        if "." in s:
            return s
        cached = self._symbol_to_secid.get(s.upper())
        if cached:
            return cached
        guessed = guess_secid_for_symbol(s)
        if guessed:
            self._symbol_to_secid[s.upper()] = guessed
            return guessed
        results = self.provider.search(s, limit=1)
        if results:
            secid = results[0]["secid"]
            self._symbol_to_secid[s.upper()] = secid
            return secid
        return None

    # ── subscription ──

    def subscribe(self, symbols_or_secids: list[str], queue: asyncio.Queue | None = None) -> list[str]:
        secids: list[str] = []
        for s in symbols_or_secids:
            secid = self.resolve_secid(s)
            if secid is None:
                logger.warning("Cannot resolve %r to a secid; skipped", s)
                continue
            secids.append(secid)
            with self._lock:
                self._active.add(secid)
                if queue is not None:
                    self._subscriptions.setdefault(secid, set()).add(queue)
        if not self._running:
            self._start()
        return secids

    def unsubscribe_queue(self, queue: asyncio.Queue) -> None:
        with self._lock:
            for subs in self._subscriptions.values():
                subs.discard(queue)

    # ── access ──

    def get_latest(self, symbol_or_secid: str) -> LiveQuote | None:
        s = symbol_or_secid.strip()
        if "." in s:
            return self._latest.get(s)
        return self._latest_by_symbol.get(s.upper())

    def get_all_latest(self) -> list[dict[str, Any]]:
        return [q.to_dict() for q in self._latest.values()]

    def fetch_now(self, secids: list[str]) -> list[LiveQuote]:
        """Synchronous fetch (used by REST detail/watchlist endpoints); also
        primes the cache and history."""
        quotes = self.provider.get_quotes(secids)
        self._ingest(quotes)
        with self._lock:
            self._active.update(secids)
        if not self._running:
            self._start()
        return quotes

    # ── anomaly signals over the actively tracked universe ──

    def anomalies(self, window_seconds: int = 600) -> list[dict[str, Any]]:
        now = time.time()
        out: list[dict[str, Any]] = []
        with self._lock:
            items = list(self._history.items())
        for secid, hist in items:
            if len(hist) < 2:
                continue
            quote = self._latest.get(secid)
            if quote is None or quote.instrument_id.asset_type == "INDEX":
                continue
            window = [h for h in hist if h[0] >= now - window_seconds]
            if len(window) < 2:
                continue
            first, last = window[0], window[-1]
            if first[1] <= 0:
                continue
            ret = (last[1] - first[1]) / first[1]
            elapsed = max(last[0] - first[0], 1.0)
            vol_rate = (last[2] - first[2]) / elapsed  # shares/sec over window
            # baseline: whole-session average rate from cumulative volume
            session_rate = 0.0
            if quote.volume > 0 and quote.market_session.value == "open":
                session_rate = quote.volume / max(4 * 3600, 1)
            vol_ratio = vol_rate / session_rate if session_rate > 0 else 0.0
            severity = "none"
            if abs(ret) >= 0.03 or vol_ratio >= 3.0:
                severity = "high"
            elif abs(ret) >= 0.015 or vol_ratio >= 2.0:
                severity = "medium"
            elif abs(ret) >= 0.008 or vol_ratio >= 1.5:
                severity = "low"
            if severity == "none":
                continue
            out.append(
                {
                    "secid": secid,
                    "symbol": quote.instrument_id.symbol,
                    "name": quote.name,
                    "window_seconds": int(elapsed),
                    "window_return": round(ret, 4),
                    "volume_ratio": round(vol_ratio, 2),
                    "severity": severity,
                    "price": quote.price,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                    "coverage": "tracked_universe",
                }
            )
        out.sort(key=lambda x: abs(x["window_return"]), reverse=True)
        return out

    # ── polling ──

    def _start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("MarketDataGateway polling started (interval=%ss)", self.poll_interval)

    def _poll_loop(self) -> None:
        while self._running:
            try:
                with self._lock:
                    secids = list(self._active)
                if secids:
                    quotes = self.provider.get_quotes(secids)
                    self._ingest(quotes)
            except Exception:
                logger.warning("Poll cycle error", exc_info=True)
            time.sleep(self.poll_interval)

    def _ingest(self, quotes: list[LiveQuote]) -> None:
        for quote in quotes:
            secid = quote.to_dict()["secid"]
            if not secid:
                continue
            self._latest[secid] = quote
            self._latest_by_symbol[quote.instrument_id.symbol.upper()] = quote
            hist = self._history.setdefault(secid, deque(maxlen=1200))
            hist.append((time.time(), quote.price, quote.volume))
            # fan out only when the exchange data actually changed
            key = (quote.timestamp, quote.price, quote.volume)
            if self._last_sent_key.get(secid) == key:
                continue
            self._last_sent_key[secid] = key
            with self._lock:
                subs = list(self._subscriptions.get(secid, set()))
            if self._loop and subs:
                payload = quote.to_dict()
                for q in subs:
                    self._loop.call_soon_threadsafe(self._enqueue_quote, q, payload)

    def _enqueue_quote(self, queue: asyncio.Queue, payload: dict[str, Any]) -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(payload)
            except asyncio.QueueEmpty:
                pass
            except asyncio.QueueFull:
                pass

    def stop(self) -> None:
        self._running = False


# Global singleton. 5s polling is polite enough that Eastmoney does not
# throttle the realtime host (3s got us blocked during development).
market_gateway = MarketDataGateway(poll_interval=5.0)
