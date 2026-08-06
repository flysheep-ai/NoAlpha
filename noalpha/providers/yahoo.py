from __future__ import annotations

import csv
import io
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from noalpha.domain import Instrument, Market, MarketBar, NewsItem, now_utc, parse_datetime
from noalpha.errors import MalformedResponseError, NotFoundError, ProviderError
from noalpha.http import HttpClient
from noalpha.provider_health import ProviderHealth, ProviderHealthStatus
from noalpha.providers.base import MarketDataProvider, NewsProvider


logger = logging.getLogger(__name__)


KNOWN_US_INSTRUMENTS = {
    "AAPL": ("0000320193", "Apple Inc.", "NASDAQ", "USD"),
    "MSFT": ("0000789019", "Microsoft Corporation", "NASDAQ", "USD"),
    "NVDA": ("0001045810", "NVIDIA Corporation", "NASDAQ", "USD"),
    "GOOGL": ("0001652044", "Alphabet Inc.", "NASDAQ", "USD"),
    "GOOG": ("0001652044", "Alphabet Inc.", "NASDAQ", "USD"),
    "AMZN": ("0001018724", "Amazon.com, Inc.", "NASDAQ", "USD"),
    "META": ("0001326801", "Meta Platforms, Inc.", "NASDAQ", "USD"),
    "TSLA": ("0001318605", "Tesla, Inc.", "NASDAQ", "USD"),
}


class YahooPrototypeMarketDataProvider(MarketDataProvider):
    """Development-only Yahoo chart provider behind a replaceable interface."""

    def __init__(
        self,
        *,
        user_agent: str = "Mozilla/5.0 NoAlpha prototype",
        cache_dir: Path | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.http = http or HttpClient(
            user_agent=user_agent,
            timeout=20,
            max_retries=2,
            retry_backoff=1.0,
            cache_dir=cache_dir,
        )

    def resolve_instrument(self, symbol: str) -> Instrument:
        normalized = symbol.upper().strip()

        # Check CN A-shares (6-digit code or secid format)
        cn_code = normalized
        if "." in cn_code:
            cn_code = cn_code.split(".")[-1]
        # Lazy import to avoid circular dependency
        from noalpha.providers.marketdata_app import KNOWN_CN_INSTRUMENTS
        cn = KNOWN_CN_INSTRUMENTS.get(cn_code)
        if cn:
            name, exchange, currency = cn
            return Instrument(cn_code, exchange, Market.A_SHARE, currency, name)

        known = KNOWN_US_INSTRUMENTS.get(normalized)
        if known:
            cik, name, exchange, currency = known
            return Instrument(normalized, exchange, Market.US, currency, name, cik=cik)

        quote = self._fetch_quote_summary(normalized)
        cik = None
        return Instrument(
            symbol=normalized,
            exchange=quote.get("exchangeName") or quote.get("fullExchangeName") or "UNKNOWN",
            market=Market.US,
            currency=quote.get("currency") or "USD",
            company_name=quote.get("longName") or quote.get("shortName") or normalized,
            sector=None,
            industry=None,
            cik=cik,
        )

    def get_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        start_ts = int(start.replace(tzinfo=timezone.utc).timestamp())
        end_ts = int(end.replace(tzinfo=timezone.utc).timestamp())
        params = urllib.parse.urlencode(
            {
                "period1": start_ts,
                "period2": end_ts,
                "interval": "1d",
                "events": "history",
                "includeAdjustedClose": "true",
            }
        )
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{instrument.symbol}?{params}"
        response = self.http.get(url, headers={"Accept": "application/json"}, cache_key=f"yahoo_chart_{instrument.symbol}_{start_ts}_{end_ts}.json")
        data = response.json()
        try:
            result = data["chart"]["result"][0]
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise MalformedResponseError(f"Yahoo chart malformed for {instrument.symbol}") from exc

        bars: list[MarketBar] = []
        observed_at = now_utc()
        for idx, ts in enumerate(timestamps):
            try:
                close = quote["close"][idx]
                if close is None:
                    continue
                timestamp = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                bars.append(
                    MarketBar(
                        instrument=instrument,
                        timestamp=timestamp,
                        open=Decimal(str(quote["open"][idx])),
                        high=Decimal(str(quote["high"][idx])),
                        low=Decimal(str(quote["low"][idx])),
                        close=Decimal(str(close)),
                        volume=int(quote["volume"][idx] or 0),
                        source="Yahoo Finance prototype chart",
                        observed_at=observed_at,
                        available_at=timestamp + timedelta(days=1),
                    )
                )
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise MalformedResponseError(f"Yahoo chart missing field for {instrument.symbol}") from exc
        if not bars:
            raise NotFoundError(f"No Yahoo bars for {instrument.symbol}")
        return bars

    def _fetch_quote_summary(self, symbol: str) -> dict[str, Any]:
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={urllib.parse.quote(symbol)}"
        response = self.http.get(url, headers={"Accept": "application/json"}, cache_key=f"yahoo_quote_{symbol}.json")
        data = response.json()
        try:
            quotes = data["quoteResponse"]["result"]
        except (KeyError, TypeError) as exc:
            raise MalformedResponseError(f"Yahoo quote malformed for {symbol}") from exc
        if not quotes:
            raise NotFoundError(f"Yahoo quote not found for {symbol}")
        return quotes[0]

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider="Yahoo Finance Charts",
            status=ProviderHealthStatus.HEALTHY,
            requires_credentials=False,
            supports_historical=True,
            supports_realtime=False,
            supports_intraday=False,
            coverage="US equities daily OHLCV via Yahoo Finance chart API. Free, no token required.",
        )


class YahooFinanceNewsProvider(NewsProvider):
    """Development news provider using Yahoo Finance RSS with attribution."""

    def __init__(
        self,
        *,
        user_agent: str = "Mozilla/5.0 NoAlpha prototype",
        cache_dir: Path | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.http = http or HttpClient(
            user_agent=user_agent,
            timeout=20,
            max_retries=2,
            retry_backoff=1.0,
            cache_dir=cache_dir,
        )

    def search_news(self, instrument: Instrument, start: datetime, end: datetime, limit: int = 20) -> list[NewsItem]:
        params = urllib.parse.urlencode({"s": instrument.symbol, "region": "US", "lang": "en-US"})
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?{params}"
        response = self.http.get(url, headers={"Accept": "application/rss+xml,text/xml,*/*"}, cache_key=f"yahoo_rss_{instrument.symbol}.xml")
        try:
            root = ElementTree.fromstring(response.body)
        except ElementTree.ParseError as exc:
            raise MalformedResponseError(f"Yahoo RSS malformed for {instrument.symbol}") from exc

        items: list[NewsItem] = []
        retrieved_at = now_utc()
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            description = (item.findtext("description") or "").strip()
            pub_text = (item.findtext("pubDate") or "").strip()
            if not title or not link or not pub_text:
                continue
            try:
                published_at = parsedate_to_datetime(pub_text)
            except (TypeError, ValueError):
                continue
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            published_at = published_at.astimezone(timezone.utc)
            if not (start <= published_at <= end):
                continue
            items.append(
                NewsItem(
                    title=title,
                    published_at=published_at,
                    retrieved_at=retrieved_at,
                    source="Yahoo Finance RSS",
                    source_url=link,
                    content=description,
                    language="en",
                    symbols=(instrument.symbol,),
                )
            )
            if len(items) >= limit:
                break
        return self._dedupe(items)

    def _dedupe(self, items: list[NewsItem]) -> list[NewsItem]:
        seen: set[str] = set()
        deduped: list[NewsItem] = []
        for item in items:
            key = (item.source_url or item.title).lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider="Yahoo Finance RSS",
            status=ProviderHealthStatus.DEGRADED,
            requires_credentials=False,
            supports_historical=False,
            supports_realtime=False,
            supports_intraday=False,
            coverage="Recent Yahoo Finance RSS items only; not a complete licensed news archive",
            reason="RSS is a prototype attribution source, not a production news feed.",
        )
