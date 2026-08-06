"""Market calendar: session times, holidays, timezone for CN/US/HK.

Every time-based decision in the 24/7 intelligence loop flows through this module.
No `if hour > ...` should remain anywhere else in the codebase.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import urllib.request

from noalpha.market.live_quote import Market, MarketSession

CN_TZ = timezone(timedelta(hours=8))
US_TZ = timezone(timedelta(hours=-4))   # Eastern, DST-naive fallback
US_TZ_STD = timezone(timedelta(hours=-5))  # Eastern Standard
HK_TZ = timezone(timedelta(hours=8))


# ── core session definitions ──

@dataclass(frozen=True)
class SessionWindow:
    label: str  # e.g. "morning_session"
    session: MarketSession
    start: time  # local time
    end: time    # local time

    def active(self, local_time: time) -> bool:
        if self.start <= self.end:
            return self.start <= local_time < self.end
        return local_time >= self.start or local_time < self.end


# Default schedules — overrideable per exchange
CN_SCHEDULE = [
    SessionWindow("morning_call", MarketSession.PRE_MARKET, time(9, 15), time(9, 30)),
    SessionWindow("morning_session", MarketSession.OPEN, time(9, 30), time(11, 30)),
    SessionWindow("lunch_break", MarketSession.BREAK, time(11, 30), time(13, 0)),
    SessionWindow("afternoon_session", MarketSession.OPEN, time(13, 0), time(15, 0)),
    SessionWindow("after_hours", MarketSession.POST_MARKET, time(15, 0), time(15, 30)),
]
US_SCHEDULE = [
    SessionWindow("pre_market_us", MarketSession.PRE_MARKET, time(4, 0), time(9, 30)),
    SessionWindow("regular_us", MarketSession.OPEN, time(9, 30), time(16, 0)),
    SessionWindow("after_hours_us", MarketSession.POST_MARKET, time(16, 0), time(20, 0)),
]
HK_SCHEDULE = [
    SessionWindow("morning_call_hk", MarketSession.PRE_MARKET, time(9, 0), time(9, 30)),
    SessionWindow("morning_session_hk", MarketSession.OPEN, time(9, 30), time(12, 0)),
    SessionWindow("lunch_hk", MarketSession.BREAK, time(12, 0), time(13, 0)),
    SessionWindow("afternoon_session_hk", MarketSession.OPEN, time(13, 0), time(16, 0)),
    SessionWindow("after_hours_hk", MarketSession.POST_MARKET, time(16, 0), time(16, 10)),
]

SCHEDULES: dict[Market, list[SessionWindow]] = {
    Market.CN: CN_SCHEDULE,
    Market.US: US_SCHEDULE,
    Market.HK: HK_SCHEDULE,
}

MARKET_TZ: dict[Market, timezone] = {
    Market.CN: CN_TZ,
    Market.US: US_TZ,
    Market.HK: HK_TZ,
}

MARKET_LABEL: dict[Market, str] = {
    Market.CN: "A 股", Market.US: "美股", Market.HK: "港股",
}


def _is_us_dst(utc_now: datetime) -> bool:
    """Best-effort US DST detection: 2nd Sun Mar – 1st Sun Nov."""
    import calendar
    yr = utc_now.year
    mar = datetime(yr, 3, 1, tzinfo=utc_now.tzinfo or timezone.utc)
    second_sun_mar = mar + timedelta(days=(6 - mar.weekday() + 7) % 7 + 7)
    nov = datetime(yr, 11, 1, tzinfo=utc_now.tzinfo or timezone.utc)
    first_sun_nov = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return second_sun_mar <= utc_now < first_sun_nov


def market_tz(market: Market, utc_now: datetime | None = None) -> timezone:
    now = utc_now or datetime.now(timezone.utc)
    if market == Market.US and _is_us_dst(now):
        return US_TZ
    if market == Market.US:
        return US_TZ_STD
    return MARKET_TZ.get(market, timezone.utc)


# ── holiday loading ──

_HOLIDAYS: dict[str, set[str]] = {}


def _load_holidays(data_dir: Path | None = None) -> None:
    """Load CN/US/HK holidays from disk (one JSON array per file, YYYY-MM-DD strings)."""
    global _HOLIDAYS
    if _HOLIDAYS:
        return
    root = data_dir or Path(".cache/api")
    for mkt, fn in [("CN", "cn_holidays.json"), ("US", "us_holidays.json"), ("HK", "hk_holidays.json")]:
        p = root / fn
        try:
            _HOLIDAYS[mkt] = set(json.loads(p.read_text()))
        except Exception:
            _HOLIDAYS[mkt] = set()


def is_holiday(market: Market, date_str: str) -> bool:
    _load_holidays()
    return date_str in _HOLIDAYS.get(market.value, set())


def is_trading_day(market: Market, local_date: str) -> bool:
    # weekends + holidays
    dt = datetime.strptime(local_date, "%Y-%m-%d")
    if dt.weekday() >= 5:
        return False
    return not is_holiday(market, local_date)


# ── session query ──

@dataclass
class MarketStatus:
    market: Market
    session: MarketSession
    session_label: str
    next_open: datetime | None
    local_time: time
    local_date: str
    is_trading_day: bool
    is_settlement_complete: bool  # for CN: after 15:30


def market_status(market: Market, utc_now: datetime | None = None) -> MarketStatus:
    now = utc_now or datetime.now(timezone.utc)
    tz = market_tz(market, now)
    local = now.astimezone(tz)
    lt = local.time()
    ld = local.strftime("%Y-%m-%d")
    trading = is_trading_day(market, ld)

    if not trading:
        return MarketStatus(
            market=market, session=MarketSession.CLOSED, session_label="holiday_or_weekend",
            next_open=_next_open(market, local), local_time=lt, local_date=ld,
            is_trading_day=False, is_settlement_complete=True,
        )

    schedule = SCHEDULES.get(market, [])
    for sw in schedule:
        if sw.active(lt):
            return MarketStatus(
                market=market, session=sw.session, session_label=sw.label,
                next_open=None, local_time=lt, local_date=ld,
                is_trading_day=True,
                is_settlement_complete=(market == Market.CN and lt >= time(15, 30)),
            )
    # outside all windows → closed
    return MarketStatus(
        market=market, session=MarketSession.CLOSED, session_label="closed",
        next_open=schedule[0].start if schedule else None,
        local_time=lt, local_date=ld,
        is_trading_day=True,
        is_settlement_complete=(market == Market.CN and lt >= time(15, 30)),
    )


def _next_open(market: Market, local: datetime) -> datetime | None:
    """Best-effort next session open for the given market. Returns UTC."""
    schedule = SCHEDULES.get(market, [])
    if not schedule:
        return None
    start_time = schedule[1].start if len(schedule) > 1 else schedule[0].start
    tz = market_tz(market)
    candidate = local.replace(hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0)
    if candidate < local:
        candidate += timedelta(days=1)
    while not is_trading_day(market, candidate.strftime("%Y-%m-%d")):
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


# ── global router ──

@dataclass
class GlobalMarketSnapshot:
    markets: dict[Market, MarketStatus]
    utc_time: datetime

    def open_markets(self) -> list[Market]:
        return [m for m, s in self.markets.items() if s.session in (MarketSession.OPEN,)]

    def closed_markets(self) -> list[Market]:
        return [m for m, s in self.markets.items() if s.session == MarketSession.CLOSED]

    def any_executable(self, allowed: list[str]) -> bool:
        allowed_set = {a.upper() for a in allowed}
        for m in self.open_markets():
            if m.value in allowed_set:
                return True
        return False

    def next_session_for(self, market: Market) -> MarketStatus | None:
        ms = self.markets.get(market)
        if ms and ms.session != MarketSession.CLOSED:
            return ms
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            m.value: {
                "session": s.session.value, "label": s.session_label,
                "local_time": s.local_time.strftime("%H:%M:%S"),
                "trading_day": s.is_trading_day,
                "next_open": s.next_open.isoformat() if s.next_open else None,
            }
            for m, s in self.markets.items()
        }


def global_snapshot(utc_now: datetime | None = None) -> GlobalMarketSnapshot:
    now = utc_now or datetime.now(timezone.utc)
    return GlobalMarketSnapshot(
        markets={m: market_status(m, now) for m in (Market.CN, Market.US, Market.HK)},
        utc_time=now,
    )
