from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from noalpha.domain import Instrument, Market, MarketBar, now_utc
from noalpha.errors import MalformedResponseError, NotFoundError, ProviderError
from noalpha.http import HttpClient
from noalpha.market.instruments import parse_secid
from noalpha.provider_health import ProviderHealth, ProviderHealthStatus
from noalpha.providers.base import MarketDataProvider
from noalpha.providers.yahoo import KNOWN_US_INSTRUMENTS


# Phase Q5: CN A-share instrument resolution for the investigation pipeline.
# Keyed by plain 6-digit code; resolve_instrument reconstructs the secid from
# the exchange prefix to feed kline providers. Company names are in Chinese —
# the LLM research engine expects them in this language when report_language=zh-CN.
KNOWN_CN_INSTRUMENTS: dict[str, tuple[str, str, str]] = {
    # Stock code → (Company name, Exchange abbreviation, Currency)
    "600519": ("贵州茅台", "SSE", "CNY"),
    "601318": ("中国平安", "SSE", "CNY"),
    "600036": ("招商银行", "SSE", "CNY"),
    "601899": ("紫金矿业", "SSE", "CNY"),
    "600900": ("长江电力", "SSE", "CNY"),
    "600030": ("中信证券", "SSE", "CNY"),
    "600276": ("恒瑞医药", "SSE", "CNY"),
    "601012": ("隆基绿能", "SSE", "CNY"),
    "600887": ("伊利股份", "SSE", "CNY"),
    "601668": ("中国建筑", "SSE", "CNY"),
    "603288": ("海天味业", "SSE", "CNY"),
    "600809": ("山西汾酒", "SSE", "CNY"),
    "601988": ("中国银行", "SSE", "CNY"),
    "000858": ("五粮液", "SZSE", "CNY"),
    "000333": ("美的集团", "SZSE", "CNY"),
    "002594": ("比亚迪", "SZSE", "CNY"),
    "000001": ("平安银行", "SZSE", "CNY"),
    "000651": ("格力电器", "SZSE", "CNY"),
    "002415": ("海康威视", "SZSE", "CNY"),
    "300059": ("东方财富", "SZSE", "CNY"),
    "002714": ("牧原股份", "SZSE", "CNY"),
    "000725": ("京东方A", "SZSE", "CNY"),
    "300750": ("宁德时代", "SZSE", "CNY"),
    "002475": ("立讯精密", "SZSE", "CNY"),
}


def _cn_secid(code: str) -> str:
    """6-digit code → full secid for kline provider lookups."""
    if code in KNOWN_CN_INSTRUMENTS:
        exchange = KNOWN_CN_INSTRUMENTS[code][1]
        prefix = "1" if exchange == "SSE" else "0"
        return f"{prefix}.{code}"
    # guess SSE for 6xx, SZSE otherwise
    prefix = "1" if code.startswith("6") else "0"
    return f"{prefix}.{code}"


logger = logging.getLogger(__name__)


class MarketDataAppPrototypeMarketDataProvider(MarketDataProvider):
    """Development-only market data provider using MarketData.app daily candles.

    This remains behind the MarketDataProvider interface because the endpoint is
    suitable for Sprint 1 validation, not a production real-time feed decision.
    """

    source_name = "MarketData.app prototype candles"

    def __init__(
        self,
        *,
        user_agent: str = "Mozilla/5.0 NoAlpha prototype",
        token: str | None = None,
        cache_dir: Path | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.token = token or os.environ.get("MARKETDATA_APP_TOKEN")
        self.http = http or HttpClient(
            user_agent=user_agent,
            timeout=20,
            max_retries=2,
            retry_backoff=1.0,
            cache_dir=cache_dir,
        )

    def resolve_instrument(self, symbol: str) -> Instrument:
        normalized = symbol.upper().strip()

        # Check CN A-shares first (6-digit code, possibly prefixed as a secid)
        cn_code = normalized
        if "." in cn_code:
            # secid format: "1.600519" → code "600519"
            cn_code = cn_code.split(".")[-1]
        cn = KNOWN_CN_INSTRUMENTS.get(cn_code)
        if cn:
            name, exchange, currency = cn
            return Instrument(cn_code, exchange, Market.A_SHARE, currency, name)

        # Check US equities
        known = KNOWN_US_INSTRUMENTS.get(normalized)
        if known:
            cik, name, exchange, currency = known
            return Instrument(normalized, exchange, Market.US, currency, name, cik=cik)

        raise NotFoundError(
            f"{normalized} is not in the instrument map; add it to "
            f"KNOWN_US_INSTRUMENTS or KNOWN_CN_INSTRUMENTS."
        )

    def get_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        # CN stocks: use kline service instead of MarketData.app
        if instrument.market == Market.A_SHARE:
            return self._get_cn_bars(instrument, start, end)
        return self._get_us_bars(instrument, start, end)

    def _get_cn_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        from noalpha.market.kline_service import KlineService
        secid = _cn_secid(instrument.symbol)
        kline = KlineService()
        result = kline.get_klines(secid, "1d", 2000)
        bars_raw = result.get("bars") or []
        if not bars_raw:
            raise NotFoundError(f"No kline bars for {instrument.symbol} ({secid}) from {result.get('source')}")

        observed_at = now_utc()
        bars: list[MarketBar] = []
        for b in bars_raw:
            bar_date = b["time"][:10]
            if bar_date < start.date().isoformat() or bar_date > end.date().isoformat():
                continue
            ts = datetime.fromisoformat(b["time"]).replace(tzinfo=timezone.utc)
            bars.append(MarketBar(
                instrument=instrument, timestamp=ts,
                open=Decimal(str(b["open"])), high=Decimal(str(b["high"])),
                low=Decimal(str(b["low"])), close=Decimal(str(b["close"])),
                volume=int(b["volume"]), source=f"tencent_qfq_via_kline_service",
                observed_at=observed_at,
                available_at=ts + timedelta(days=1),
            ))
        if not bars:
            raise NotFoundError(f"No kline bars for {instrument.symbol} in range {start.date()}..{end.date()}")
        return bars

    def _get_us_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        start_date = start.astimezone(timezone.utc).date().isoformat()
        end_date = end.astimezone(timezone.utc).date().isoformat()
        symbol = urllib.parse.quote(instrument.symbol)
        params = urllib.parse.urlencode({"from": start_date, "to": end_date})
        url = f"https://api.marketdata.app/v1/stocks/candles/D/{symbol}/?{params}"
        response = self.http.get(
            url,
            headers=self._headers(),
            cache_key=f"marketdata_app_daily_{instrument.symbol}_{start_date}_{end_date}.json",
        )
        data = response.json()
        if data.get("s") not in {"ok", "no_data"}:
            message = data.get("errmsg") or data.get("message") or "unknown provider status"
            raise ProviderError(f"MarketData.app failed for {instrument.symbol}: {message}")
        if data.get("s") == "no_data":
            raise NotFoundError(f"No MarketData.app bars for {instrument.symbol}")
        return self._parse_bars(instrument, data)

    def _parse_bars(self, instrument: Instrument, data: dict[str, Any]) -> list[MarketBar]:
        required = ("t", "o", "h", "l", "c", "v")
        if any(name not in data or not isinstance(data[name], list) for name in required):
            raise MalformedResponseError(f"MarketData.app response missing OHLCV arrays for {instrument.symbol}")
        lengths = {len(data[name]) for name in required}
        if len(lengths) != 1:
            raise MalformedResponseError(f"MarketData.app OHLCV arrays have inconsistent lengths for {instrument.symbol}")

        observed_at = now_utc()
        bars: list[MarketBar] = []
        for idx, ts in enumerate(data["t"]):
            try:
                timestamp = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                close = data["c"][idx]
                if close is None:
                    continue
                bars.append(
                    MarketBar(
                        instrument=instrument,
                        timestamp=timestamp,
                        open=Decimal(str(data["o"][idx])),
                        high=Decimal(str(data["h"][idx])),
                        low=Decimal(str(data["l"][idx])),
                        close=Decimal(str(close)),
                        volume=int(data["v"][idx] or 0),
                        source=self.source_name,
                        observed_at=observed_at,
                        available_at=timestamp + timedelta(days=1),
                    )
                )
            except (TypeError, ValueError, IndexError) as exc:
                raise MalformedResponseError(f"MarketData.app malformed bar for {instrument.symbol}") from exc
        if not bars:
            raise NotFoundError(f"No usable MarketData.app bars for {instrument.symbol}")
        return bars

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def health(self) -> ProviderHealth:
        if self.token:
            return ProviderHealth(
                provider=self.source_name,
                status=ProviderHealthStatus.HEALTHY,
                requires_credentials=True,
                supports_historical=True,
                supports_realtime=False,
                supports_intraday=False,
                coverage="US equities subject to subscription/token entitlements",
            )
        return ProviderHealth(
            provider=self.source_name,
            status=ProviderHealthStatus.DEGRADED,
            requires_credentials=True,
            supports_historical=True,
            supports_realtime=False,
            supports_intraday=False,
            coverage="No-token mode has limited observed coverage; AAPL worked, NVDA/MSFT returned unauthorized.",
            reason="MARKETDATA_APP_TOKEN is not configured.",
        )
