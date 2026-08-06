"""Cross-market instrument identity, market sessions and freshness.

secid is the canonical wire id used by Eastmoney: "<mkt>.<code>", e.g.
"1.600519" (SSE), "0.300750" (SZSE), "105.AAPL" (NASDAQ), "106.KO" (NYSE),
"100.SPX" (global index). All new market APIs key on secid so CN/US
instruments coexist in one watchlist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from noalpha.market.live_quote import InstrumentId, Market, MarketSession

CN_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")

_EXCHANGES = {
    "1": ("SSE", Market.CN, "CNY"),
    "0": ("SZSE", Market.CN, "CNY"),
    "105": ("NASDAQ", Market.US, "USD"),
    "106": ("NYSE", Market.US, "USD"),
    "107": ("AMEX", Market.US, "USD"),
    "100": ("INDEX", Market.US, "USD"),  # Eastmoney global-index namespace
    "116": ("HKEX", Market.HK, "HKD"),
}

# Index secids shown on the market overview. Only indices the provider
# actually serves are listed; nothing is fabricated client-side.
CN_INDEX_SECIDS = ["1.000001", "0.399001", "0.399006", "1.000688"]
US_INDEX_SECIDS = ["100.SPX", "100.NDX", "100.DJIA"]
INDEX_SECIDS = set(CN_INDEX_SECIDS + US_INDEX_SECIDS)


@dataclass(frozen=True)
class SecId:
    market_code: str  # "1", "0", "105", ...
    code: str  # "600519", "AAPL"

    @property
    def secid(self) -> str:
        return f"{self.market_code}.{self.code}"

    @property
    def market(self) -> Market:
        return _EXCHANGES.get(self.market_code, ("UNKNOWN", Market.US, "USD"))[1]

    @property
    def exchange(self) -> str:
        return _EXCHANGES.get(self.market_code, ("UNKNOWN", Market.US, "USD"))[0]

    @property
    def currency(self) -> str:
        return _EXCHANGES.get(self.market_code, ("UNKNOWN", Market.US, "USD"))[2]

    @property
    def is_index(self) -> bool:
        return self.secid in INDEX_SECIDS

    def instrument_id(self) -> InstrumentId:
        return InstrumentId(
            symbol=self.code,
            exchange=self.exchange,
            market=self.market,
            asset_type="INDEX" if self.is_index else "EQUITY",
            currency=self.currency,
        )


def parse_secid(secid: str) -> SecId | None:
    if "." not in secid:
        return None
    market_code, _, code = secid.partition(".")
    if not market_code or not code:
        return None
    return SecId(market_code=market_code, code=code.upper() if not code.isdigit() else code)


def guess_secid_for_symbol(symbol: str) -> str | None:
    """Best-effort mapping from a bare symbol to a secid (used by legacy
    callers that only have "600519"/"AAPL"). CN codes are 6 digits and the
    exchange is derivable from the leading digit; anything alphabetic is
    assumed US NASDAQ-or-NYSE and must be resolved via search instead."""
    s = symbol.strip().upper()
    if s.isdigit() and len(s) == 6:
        return f"1.{s}" if s[0] in ("6", "9", "5") else f"0.{s}"
    return None


def market_session(market: Market, now: datetime | None = None) -> MarketSession:
    now = now or datetime.now(timezone.utc)
    if market == Market.CN:
        local = now.astimezone(CN_TZ)
        if local.weekday() >= 5:
            return MarketSession.CLOSED
        minutes = local.hour * 60 + local.minute
        if minutes < 9 * 60 + 15:
            return MarketSession.CLOSED
        if minutes < 9 * 60 + 30:
            return MarketSession.PRE_MARKET
        if minutes < 11 * 60 + 30:
            return MarketSession.OPEN
        if minutes < 13 * 60:
            return MarketSession.BREAK
        if minutes < 15 * 60:
            return MarketSession.OPEN
        return MarketSession.CLOSED
    if market == Market.US:
        local = now.astimezone(US_TZ)
        if local.weekday() >= 5:
            return MarketSession.CLOSED
        minutes = local.hour * 60 + local.minute
        if minutes < 4 * 60:
            return MarketSession.CLOSED
        if minutes < 9 * 60 + 30:
            return MarketSession.PRE_MARKET
        if minutes < 16 * 60:
            return MarketSession.OPEN
        if minutes < 20 * 60:
            return MarketSession.POST_MARKET
        return MarketSession.CLOSED
    return MarketSession.UNKNOWN


def compute_freshness(
    market: Market, quote_ts: datetime | None, now: datetime | None = None
) -> tuple[str, float, MarketSession]:
    """Freshness derived from the *exchange* timestamp, never wall-clock.

    Returns (freshness, delay_seconds, session). freshness is one of
    live / delayed / stale / closed / unknown.
    """
    now = now or datetime.now(timezone.utc)
    session = market_session(market, now)
    if quote_ts is None:
        return "unknown", -1.0, session
    delay = (now - quote_ts).total_seconds()
    if session == MarketSession.CLOSED:
        return "closed", delay, session
    # OPEN plus auction/extended sessions that stream real ticks
    if delay <= 60:
        return "live", delay, session
    if delay <= 900:
        return "delayed", delay, session
    if session != MarketSession.OPEN:
        return "closed", delay, session
    return "stale", delay, session
