"""Persistent cross-market watchlist (SQLite, same DB file as watch.sqlite3).

Separate table from the watch service's `watch_items` because entries here
are keyed by secid (cross-market) and carry display ordering; the legacy
scan-policy watchlist remains untouched.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SEEDS = [
    # Previously hardcoded CN names plus a small US starter set; fully
    # user-editable afterwards (add/remove/enable/disable/reorder).
    ("1.600519", "600519", "贵州茅台", "CN", "SSE", "CNY"),
    ("0.000001", "000001", "平安银行", "CN", "SZSE", "CNY"),
    ("0.300750", "300750", "宁德时代", "CN", "SZSE", "CNY"),
    ("0.002463", "002463", "沪电股份", "CN", "SZSE", "CNY"),
    ("105.AAPL", "AAPL", "苹果", "US", "NASDAQ", "USD"),
    ("105.NVDA", "NVDA", "英伟达", "US", "NASDAQ", "USD"),
    ("105.MSFT", "MSFT", "微软", "US", "NASDAQ", "USD"),
]


class MarketWatchlistStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                create table if not exists market_watchlist (
                  secid text primary key,
                  symbol text not null,
                  name text not null,
                  market text not null,
                  exchange text not null,
                  currency text not null,
                  enabled integer not null default 1,
                  sort_order integer not null default 0,
                  added_at text not null
                );
                """
            )
            count = conn.execute("select count(*) from market_watchlist").fetchone()[0]
            if count == 0:
                now = datetime.now(timezone.utc).isoformat()
                for order, (secid, symbol, name, market, exchange, currency) in enumerate(DEFAULT_SEEDS):
                    conn.execute(
                        "insert into market_watchlist(secid,symbol,name,market,exchange,currency,enabled,sort_order,added_at)"
                        " values(?,?,?,?,?,?,1,?,?)",
                        (secid, symbol, name, market, exchange, currency, order, now),
                    )

    def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "select * from market_watchlist"
        if enabled_only:
            query += " where enabled=1"
        query += " order by sort_order, added_at"
        with self._lock, self._conn() as conn:
            rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]

    def add(self, secid: str, symbol: str, name: str, market: str, exchange: str, currency: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            max_order = conn.execute("select coalesce(max(sort_order),-1) from market_watchlist").fetchone()[0]
            conn.execute(
                "insert into market_watchlist(secid,symbol,name,market,exchange,currency,enabled,sort_order,added_at)"
                " values(?,?,?,?,?,?,1,?,?)"
                " on conflict(secid) do update set enabled=1, name=excluded.name",
                (secid, symbol, name, market, exchange, currency, max_order + 1, now),
            )
        return {"secid": secid, "symbol": symbol, "name": name, "market": market,
                "exchange": exchange, "currency": currency, "enabled": 1}

    def remove(self, secid: str) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute("delete from market_watchlist where secid=?", (secid,))
        return cur.rowcount > 0

    def set_enabled(self, secid: str, enabled: bool) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "update market_watchlist set enabled=? where secid=?", (1 if enabled else 0, secid)
            )
        return cur.rowcount > 0

    def reorder(self, secids: list[str]) -> None:
        with self._lock, self._conn() as conn:
            for order, secid in enumerate(secids):
                conn.execute("update market_watchlist set sort_order=? where secid=?", (order, secid))
