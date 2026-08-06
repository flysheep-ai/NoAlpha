from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Market(str, Enum):
    US = "US"
    CN = "CN"
    HK = "HK"


class MarketSession(str, Enum):
    PRE_MARKET = "pre_market"
    OPEN = "open"
    BREAK = "break"
    POST_MARKET = "post_market"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InstrumentId:
    symbol: str
    exchange: str  # SSE, SZSE, NASDAQ, NYSE
    market: Market
    asset_type: str = "EQUITY"
    currency: str = "CNY"
    canonical: str = ""

    def __post_init__(self):
        if not self.canonical:
            object.__setattr__(self, "canonical",
                f"{self.market.value}:{self.exchange}:{self.symbol}")


@dataclass
class LiveQuote:
    instrument_id: InstrumentId
    name: str = ""
    price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    previous_close: float = 0.0
    change: float = 0.0
    change_percent: float = 0.0
    volume: float = 0.0
    turnover: float = 0.0
    timestamp: str = ""
    received_at: str = ""
    source: str = ""
    freshness: str = "unknown"  # live / delayed / stale
    delay_seconds: float = 0.0
    market_session: MarketSession = MarketSession.UNKNOWN
    exchange_timezone: str = "Asia/Shanghai"

    def to_dict(self) -> dict[str, Any]:
        exchange_to_market_code = {
            "SSE": "1", "SZSE": "0", "NASDAQ": "105", "NYSE": "106",
            "AMEX": "107", "INDEX": "100", "HKEX": "116",
        }
        mkt_code = exchange_to_market_code.get(self.instrument_id.exchange, "")
        return {
            "secid": f"{mkt_code}.{self.instrument_id.symbol}" if mkt_code else "",
            "symbol": self.instrument_id.symbol,
            "exchange": self.instrument_id.exchange,
            "market": self.instrument_id.market.value,
            "canonical": self.instrument_id.canonical,
            "name": self.name,
            "price": self.price,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "previous_close": self.previous_close,
            "change": round(self.change, 2),
            "change_percent": round(self.change_percent, 2),
            "volume": self.volume,
            "turnover": self.turnover,
            "timestamp": self.timestamp,
            "received_at": self.received_at,
            "source": self.source,
            "currency": self.instrument_id.currency,
            "asset_type": self.instrument_id.asset_type,
            "freshness": self.freshness,
            "delay_seconds": self.delay_seconds,
            "market_session": self.market_session.value,
        }
