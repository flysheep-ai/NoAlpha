from __future__ import annotations

import logging
import urllib.request
from datetime import datetime, timezone
from typing import Any

from noalpha.market.live_quote import InstrumentId, LiveQuote, Market, MarketSession

logger = logging.getLogger(__name__)

# Sina Finance API mapping
SINA_SYMBOL_MAP: dict[str, InstrumentId] = {
    "sh600519": InstrumentId("600519", "SSE", Market.CN, currency="CNY"),
    "sh600036": InstrumentId("600036", "SSE", Market.CN, currency="CNY"),
    "sh601318": InstrumentId("601318", "SSE", Market.CN, currency="CNY"),
    "sz000001": InstrumentId("000001", "SZSE", Market.CN, currency="CNY"),
    "sz000858": InstrumentId("000858", "SZSE", Market.CN, currency="CNY"),
    "sz300750": InstrumentId("300750", "SZSE", Market.CN, currency="CNY"),
    "sz002463": InstrumentId("002463", "SZSE", Market.CN, currency="CNY"),
}

SINA_INDEX_MAP: dict[str, str] = {
    "s_sh000001": "上证指数",
    "s_sz399001": "深证成指",
    "s_sz399006": "创业板指",
    "s_sh000688": "科创50",
}


class SinaMarketProvider:
    """A-share real-time quotes via Sina Finance HTTP API. Free, no auth required."""

    BASE_URL = "https://hq.sinajs.cn/list="
    HEADERS = {"Referer": "https://finance.sina.com.cn"}

    def get_quotes(self, sina_codes: list[str]) -> list[LiveQuote]:
        if not sina_codes:
            return []
        url = self.BASE_URL + ",".join(sina_codes)
        try:
            req = urllib.request.Request(url, headers=self.HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("gbk")
        except Exception as exc:
            logger.warning("Sina fetch failed: %s", exc)
            return []

        quotes: list[LiveQuote] = []
        now = datetime.now(timezone.utc).isoformat()
        for line in raw.strip().split("\n"):
            if not line.strip() or "=" not in line:
                continue
            try:
                quote = self._parse_line(line, sina_codes, now)
                if quote:
                    quotes.append(quote)
            except Exception:
                continue
        return quotes

    def _parse_line(self, line: str, codes: list[str], now: str) -> LiveQuote | None:
        # Extract the sina code prefix
        prefix = line.split("=")[0].replace("var hq_str_", "").strip()
        inst = SINA_SYMBOL_MAP.get(prefix)
        if not inst:
            # Check if it's an index
            name = SINA_INDEX_MAP.get(prefix)
            if name:
                return self._parse_index(line, prefix, name, now)
            return None

        data = line.split('"')[1].split(",") if '"' in line else []
        if len(data) < 32:
            return None

        name = data[0]
        open_p = float(data[1]) if data[1] else 0.0
        prev_close = float(data[2]) if data[2] else 0.0
        price = float(data[3]) if data[3] else 0.0
        high = float(data[4]) if data[4] else 0.0
        low = float(data[5]) if data[5] else 0.0
        volume = float(data[8]) if data[8] else 0.0  # shares
        turnover = float(data[9]) if data[9] else 0.0  # CNY

        change = price - prev_close if prev_close else 0
        change_pct = (change / prev_close * 100) if prev_close else 0

        session = MarketSession.UNKNOWN
        # CN market hours: 9:30-11:30, 13:00-15:00 Beijing time
        bj_hour = (datetime.now().hour + 8) % 24  # rough estimate
        if 9 <= bj_hour < 11 or (bj_hour == 11 and datetime.now().minute < 30):
            session = MarketSession.OPEN
        elif 13 <= bj_hour < 15:
            session = MarketSession.OPEN
        elif bj_hour < 9 or (bj_hour == 9 and datetime.now().minute < 30):
            session = MarketSession.PRE_MARKET
        elif 11 <= bj_hour < 13:
            session = MarketSession.BREAK
        elif bj_hour >= 15:
            session = MarketSession.CLOSED

        return LiveQuote(
            instrument_id=inst, name=name, price=price, open=open_p,
            high=high, low=low, previous_close=prev_close,
            change=change, change_percent=change_pct,
            volume=volume, turnover=turnover,
            timestamp=now, received_at=now,
            source="sina", freshness="live", delay_seconds=3,
            market_session=session, exchange_timezone="Asia/Shanghai",
        )

    def _parse_index(self, line: str, code: str, name: str, now: str) -> LiveQuote | None:
        data = line.split('"')[1].split(",") if '"' in line else []
        if len(data) < 6:
            return None
        price = float(data[1]) if data[1] else 0.0
        change = float(data[2]) if data[2] else 0.0
        change_pct = float(data[3]) if data[3] else 0.0
        volume = float(data[4]) if data[4] else 0.0
        turnover = float(data[5]) if data[5] else 0.0

        symbol = code.removeprefix("s_sh").removeprefix("s_sz")
        inst = InstrumentId(symbol,
                           "SSE" if "sh" in code else "SZSE", Market.CN, currency="CNY")
        return LiveQuote(
            instrument_id=inst, name=name, price=price,
            change=change, change_percent=change_pct,
            volume=volume, turnover=turnover,
            timestamp=now, received_at=now,
            source="sina", freshness="live", delay_seconds=3,
            exchange_timezone="Asia/Shanghai",
        )

    @staticmethod
    def get_index_codes() -> list[str]:
        return list(SINA_INDEX_MAP.keys())

    @staticmethod
    def get_watchlist_codes(symbols: list[str]) -> list[str]:
        codes = []
        for s in symbols:
            for code, inst in SINA_SYMBOL_MAP.items():
                if inst.symbol == s:
                    codes.append(code)
        return codes
