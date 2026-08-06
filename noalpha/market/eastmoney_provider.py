"""Eastmoney push2 market data: instrument search, cross-market quotes, movers.

All numbers come straight from Eastmoney's public quote API (the same feed
that powers quote.eastmoney.com). Exchange timestamps (f86/f124, unix
seconds) are preserved so freshness is computed from real data times, not
the local clock. A-share quotes are exchange real-time; US quotes may be
delayed by the upstream — delay is exposed, never hidden.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from noalpha.market.instruments import SecId, compute_freshness, parse_secid
from noalpha.market.live_quote import LiveQuote, Market

logger = logging.getLogger(__name__)

SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"
# Realtime host first; the delay host serves US/global indices when the
# realtime host throttles us. CN rows the delay host cannot serve come back
# as "-" and are backfilled from Sina (true exchange timestamps).
QUOTE_HOSTS = [
    "https://push2.eastmoney.com/api/qt/ulist.np/get",
    "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
]
CLIST_HOSTS = [
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
]
SEARCH_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"  # public web token

QUOTE_FIELDS = "f2,f3,f4,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18,f124"

_ACCEPTED_CLASSIFY = {"AStock", "UsStock"}


def _num(value: Any, default: float = 0.0) -> float:
    """Eastmoney sends '-' for unavailable numeric fields."""
    if value is None or value == "-" or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _http_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Sina index code prefixes: CN indices use full-format sh000001/sz399001.
_SINA_TZ_OFFSET = "+08:00"


def _sina_cn_quotes(secs: list["SecId"], received_at: datetime) -> list[LiveQuote]:
    """CN realtime quotes from Sina hq API, preserving the exchange's own
    date/time fields (data[30]/data[31], Asia/Shanghai)."""
    if not secs:
        return []
    codes = [("sh" if s.market_code == "1" else "sz") + s.code for s in secs]
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk", "ignore")
    except Exception as exc:
        logger.warning("Sina CN backfill failed: %s", exc)
        return []
    quotes: list[LiveQuote] = []
    by_code = {("sh" if s.market_code == "1" else "sz") + s.code: s for s in secs}
    for line in raw.strip().split("\n"):
        if "=" not in line or '"' not in line:
            continue
        prefix = line.split("=")[0].replace("var hq_str_", "").strip()
        sec = by_code.get(prefix)
        data = line.split('"')[1].split(",")
        if sec is None or len(data) < 32:
            continue
        try:
            quote_ts = datetime.fromisoformat(f"{data[30]}T{data[31]}{_SINA_TZ_OFFSET}").astimezone(timezone.utc)
        except ValueError:
            quote_ts = None
        freshness, delay, session = compute_freshness(sec.market, quote_ts, received_at)
        prev_close = float(data[2]) if data[2] else 0.0
        price = float(data[3]) if data[3] else 0.0
        if price <= 0 and prev_close > 0:
            # Pre-open: no trade yet today; last traded price is prev close.
            price = prev_close
        change = price - prev_close if prev_close else 0.0
        quotes.append(LiveQuote(
            instrument_id=sec.instrument_id(),
            name=data[0],
            price=price,
            open=float(data[1]) if data[1] else 0.0,
            high=float(data[4]) if data[4] else 0.0,
            low=float(data[5]) if data[5] else 0.0,
            previous_close=prev_close,
            change=change,
            change_percent=(change / prev_close * 100) if prev_close else 0.0,
            volume=float(data[8]) if data[8] else 0.0,
            turnover=float(data[9]) if data[9] else 0.0,
            timestamp=quote_ts.isoformat() if quote_ts else "",
            received_at=received_at.isoformat(),
            source="sina",
            freshness=freshness,
            delay_seconds=round(delay, 1),
            market_session=session,
            exchange_timezone="Asia/Shanghai",
        ))
    return quotes


class EastmoneyProvider:
    """Search + realtime quotes + market movers for CN and US equities."""

    source_name = "eastmoney"

    def search(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode(
            {"input": keyword, "type": "14", "token": SEARCH_TOKEN, "count": str(min(limit * 3, 30))}
        )
        try:
            data = _http_json(f"{SEARCH_URL}?{params}")
        except Exception as exc:
            logger.warning("Eastmoney search failed for %r: %s", keyword, exc)
            return []
        rows = (data.get("QuotationCodeTable") or {}).get("Data") or []
        results: list[dict[str, Any]] = []
        for row in rows:
            if row.get("Classify") not in _ACCEPTED_CLASSIFY:
                continue
            quote_id = row.get("QuoteID") or ""
            sec = parse_secid(quote_id)
            if sec is None:
                continue
            results.append(
                {
                    "secid": sec.secid,
                    "symbol": row.get("Code", ""),
                    "name": row.get("Name", ""),
                    "pinyin": row.get("PinYin", ""),
                    "market": sec.market.value,
                    "exchange": sec.exchange,
                    "currency": sec.currency,
                    "security_type": row.get("SecurityTypeName", ""),
                }
            )
            if len(results) >= limit:
                break
        return results

    def get_quotes(self, secids: list[str]) -> list[LiveQuote]:
        if not secids:
            return []
        params = urllib.parse.urlencode(
            {"secids": ",".join(secids), "fields": QUOTE_FIELDS, "fltt": "2", "invt": "2"}
        )
        rows: list[dict[str, Any]] = []
        for host in QUOTE_HOSTS:
            try:
                data = _http_json(f"{host}?{params}")
                rows = (data.get("data") or {}).get("diff") or []
                if rows:
                    break
            except Exception as exc:
                logger.warning("Eastmoney quotes via %s failed: %s", host.split("/")[2], exc)
        received_at = datetime.now(timezone.utc)
        quotes: list[LiveQuote] = []
        for row in rows:
            quote = self._row_to_quote(row, received_at)
            if quote is not None:
                quotes.append(quote)
        # CN symbols the EM hosts could not serve (throttled realtime host,
        # '-' fields on the delay host) → backfill from Sina with real
        # exchange timestamps. Nothing is fabricated: missing stays missing.
        served = {q.to_dict()["secid"] for q in quotes}
        missing_cn = [
            sec for secid in secids
            if secid not in served
            and (sec := parse_secid(secid)) is not None and sec.market == Market.CN
        ]
        if missing_cn:
            quotes.extend(_sina_cn_quotes(missing_cn, received_at))
        return quotes

    def _row_to_quote(self, row: dict[str, Any], received_at: datetime) -> LiveQuote | None:
        code = str(row.get("f12") or "")
        market_code = str(row.get("f13") if row.get("f13") is not None else "")
        if not code or not market_code:
            return None
        if row.get("f2") in (None, "-", ""):
            return None  # host declined this row (e.g. delay host + CN realtime)
        sec = SecId(market_code=market_code, code=code)
        ts_unix = _num(row.get("f124"))
        quote_ts = (
            datetime.fromtimestamp(int(ts_unix), tz=timezone.utc) if ts_unix > 0 else None
        )
        freshness, delay, session = compute_freshness(sec.market, quote_ts, received_at)
        price = _num(row.get("f2"))
        prev_close = _num(row.get("f18"))
        return LiveQuote(
            instrument_id=sec.instrument_id(),
            name=str(row.get("f14") or ""),
            price=price,
            open=_num(row.get("f17")),
            high=_num(row.get("f15")),
            low=_num(row.get("f16")),
            previous_close=prev_close,
            change=_num(row.get("f4")),
            change_percent=_num(row.get("f3")),
            volume=_num(row.get("f5")),
            turnover=_num(row.get("f6")),
            timestamp=quote_ts.isoformat() if quote_ts else "",
            received_at=received_at.isoformat(),
            source=self.source_name,
            freshness=freshness,
            delay_seconds=round(delay, 1),
            market_session=session,
            exchange_timezone="Asia/Shanghai" if sec.market == Market.CN else "America/New_York",
        )

    def get_movers(self, market: str = "CN", rank: str = "gainers", limit: int = 10) -> dict[str, Any]:
        """Whole-market rankings. fid f3=change%, f5=volume, f6=turnover."""
        fs = (
            "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
            if market.upper() == "CN"
            else "m:105,m:106,m:107"
        )
        fid = {"gainers": "f3", "losers": "f3", "volume": "f5", "turnover": "f6"}.get(rank, "f3")
        po = "0" if rank == "losers" else "1"
        params = urllib.parse.urlencode(
            {
                "pn": "1", "pz": str(limit), "po": po, "np": "1",
                "fltt": "2", "invt": "2", "fid": fid, "fs": fs,
                "fields": QUOTE_FIELDS,
            }
        )
        rows: list[dict[str, Any]] = []
        last_error = ""
        for host in CLIST_HOSTS:
            try:
                data = _http_json(f"{host}?{params}")
                rows = (data.get("data") or {}).get("diff") or []
                if rows:
                    break
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Eastmoney movers via %s failed: %s", host.split("/")[2], exc)
        if not rows:
            return {"coverage": "unavailable", "items": [], "error": last_error or "no data"}
        received_at = datetime.now(timezone.utc)
        items = []
        for row in rows:
            quote = self._row_to_quote(row, received_at)
            if quote is not None:
                items.append(quote.to_dict() | {"name": quote.name})
        return {
            "coverage": "full_market",
            "market": market.upper(),
            "rank": rank,
            "source": self.source_name,
            "items": items,
        }
