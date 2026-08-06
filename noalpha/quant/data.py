"""Long-horizon historical daily bars for quant research.

The realtime KlineService tops out at one page (~640 bars). Research needs
years, so this service pages the Tencent fqkline/usfqkline endpoints backwards
by end-date (640 bars per page, qfq-adjusted for both CN and US — verified
2026-07-07 from this machine) and caches to disk so experiments are
reproducible offline. No bar is ever synthesized.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Any

from noalpha.market.instruments import parse_secid
from noalpha.market.live_quote import Market

logger = logging.getLogger(__name__)

PAGE = 640


def _http_json(url: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _tencent_code(secid: str) -> tuple[str, str]:
    """Returns (code, endpoint) for tencent kline endpoints."""
    sec = parse_secid(secid)
    if sec is None:
        raise ValueError(f"bad secid {secid}")
    if sec.market == Market.CN:
        return ("sh" if sec.market_code == "1" else "sz") + sec.code, "fqkline"
    if sec.market == Market.US:
        suffix = ".OQ" if sec.exchange == "NASDAQ" else ".N"
        return f"us{sec.code}{suffix}", "usfqkline"
    raise ValueError(f"unsupported market for {secid}")


def _fetch_page(code: str, endpoint: str, end_date: str, limit: int = PAGE) -> list[dict[str, Any]]:
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/{endpoint}/get?"
           f"param={code},day,,{end_date},{limit},qfq")
    data = _http_json(url)
    payload = (data.get("data") or {}).get(code) or {}
    rows = payload.get("qfqday") or payload.get("day") or []
    bars = []
    for r in rows:
        bars.append({"time": r[0], "open": float(r[1]), "close": float(r[2]),
                     "high": float(r[3]), "low": float(r[4]),
                     "volume": float(r[5]) if len(r) > 5 else 0.0, "turnover": 0.0})
    return bars


class HistoricalDataService:
    def __init__(self, cache_dir: str | Path = ".cache/quant/bars") -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, secid: str) -> Path:
        return self.cache_dir / f"{secid.replace('.', '_')}.json"

    def get_daily_history(self, secid: str, start_date: str, end_date: str,
                          use_cache: bool = True) -> list[dict[str, Any]]:
        """All daily bars in [start_date, end_date], ascending. Pages backwards
        until start_date is covered or the provider runs out of history."""
        cached: list[dict[str, Any]] = []
        path = self._cache_path(secid)
        if use_cache and path.exists():
            cached = json.loads(path.read_text())
            if cached and cached[0]["time"] <= start_date and cached[-1]["time"] >= end_date:
                return [b for b in cached if start_date <= b["time"] <= end_date]

        code, endpoint = _tencent_code(secid)
        by_date: dict[str, dict[str, Any]] = {b["time"]: b for b in cached}
        cursor = ""  # empty end date = latest
        for _ in range(12):  # 12 pages ≈ 30 years, hard stop
            bars = _fetch_page(code, endpoint, cursor)
            if not bars:
                break
            new = 0
            for b in bars:
                if b["time"] not in by_date:
                    by_date[b["time"]] = b
                    new += 1
            first = bars[0]["time"]
            if first <= start_date or new == 0:
                break
            # next page ends the day before the earliest bar we have
            cursor = first
            time.sleep(0.3)  # be polite; provider rate limits are real (see memory)
        merged = sorted(by_date.values(), key=lambda b: b["time"])
        if merged:
            path.write_text(json.dumps(merged))
        return [b for b in merged if start_date <= b["time"] <= end_date]

    def get_universe_history(self, secids: list[str], start_date: str, end_date: str,
                             min_bars: int = 100) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """History per secid; instruments with insufficient real data are
        reported in `skipped`, never padded."""
        history: dict[str, list[dict[str, Any]]] = {}
        skipped: list[str] = []
        for secid in secids:
            try:
                bars = self.get_daily_history(secid, start_date, end_date)
            except Exception as exc:
                logger.warning("history fetch failed for %s: %s", secid, exc)
                skipped.append(f"{secid}: {exc}")
                continue
            if len(bars) < min_bars:
                skipped.append(f"{secid}: only {len(bars)} bars")
                continue
            history[secid] = bars
        return history, skipped
