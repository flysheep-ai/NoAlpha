"""Real historical/intraday candles with a provider fallback chain.

No candle is ever synthesized. Each (market, period) pair maps to the
providers that actually serve it from this machine:

  CN 5m/15m/30m/60m : Sina CN_MarketDataService (real exchange bars)
  CN 1d             : Tencent ifzq fqkline (qfq), fallback Sina scale=240
  US 1d             : Tencent ifzq usfqkline (qfq)
  CN/US 1m..60m/1d  : Eastmoney push2his, tried first when reachable

If every provider for a period fails, the API reports the period as
unavailable and the frontend disables it — it never draws fake candles.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from typing import Any

from noalpha.market.instruments import parse_secid
from noalpha.market.live_quote import Market

logger = logging.getLogger(__name__)

PERIODS = ["1m", "5m", "15m", "60m", "1d"]

_EM_KLT = {"1m": "1", "5m": "5", "15m": "15", "60m": "60", "1d": "101"}
_SINA_SCALE = {"5m": "5", "15m": "15", "30m": "30", "60m": "60", "1d": "240"}


def _http_text(url: str, timeout: float = 12.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _bar(t: str, o: float, h: float, low: float, c: float, v: float, turnover: float = 0.0) -> dict[str, Any]:
    return {"time": t, "open": o, "high": h, "low": low, "close": c, "volume": v, "turnover": turnover}


class KlineError(Exception):
    pass


def _eastmoney_klines(secid: str, period: str, limit: int) -> list[dict[str, Any]]:
    klt = _EM_KLT.get(period)
    if klt is None:
        raise KlineError(f"eastmoney does not serve period {period}")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&klt={klt}&fqt=1&fields1=f1,f2,f3&"
        "fields2=f51,f52,f53,f54,f55,f56,f57&beg=0&end=20500101&lmt=" + str(limit)
    )
    data = json.loads(_http_text(url))
    lines = ((data.get("data") or {}).get("klines")) or []
    bars = []
    for line in lines[-limit:]:
        p = line.split(",")
        # f51 date, f52 open, f53 close, f54 high, f55 low, f56 volume, f57 turnover
        bars.append(_bar(p[0], float(p[1]), float(p[3]), float(p[4]), float(p[2]), float(p[5]), float(p[6])))
    if not bars:
        raise KlineError("eastmoney returned no bars")
    return bars


def _sina_cn_klines(secid: str, period: str, limit: int) -> list[dict[str, Any]]:
    sec = parse_secid(secid)
    if sec is None or sec.market != Market.CN:
        raise KlineError("sina serves CN only")
    scale = _SINA_SCALE.get(period)
    if scale is None:
        raise KlineError(f"sina does not serve period {period}")
    prefix = "sh" if sec.market_code == "1" else "sz"
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20d=/CN_MarketDataService.getKLineData?"
        f"symbol={prefix}{sec.code}&scale={scale}&ma=no&datalen={min(limit, 1023)}"
    )
    raw = _http_text(url)
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise KlineError("sina response not parseable")
    rows = json.loads(match.group(0))
    bars = [
        _bar(r["day"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]),
             float(r.get("amount", 0) or 0))
        for r in rows
    ]
    if not bars:
        raise KlineError("sina returned no bars")
    return bars[-limit:]


def _tencent_daily(secid: str, period: str, limit: int) -> list[dict[str, Any]]:
    if period != "1d":
        raise KlineError("tencent chain used for daily only")
    sec = parse_secid(secid)
    if sec is None:
        raise KlineError("bad secid")
    if sec.market == Market.CN:
        code = ("sh" if sec.market_code == "1" else "sz") + sec.code
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{limit},qfq"
    elif sec.market == Market.US:
        suffix = ".OQ" if sec.exchange == "NASDAQ" else ".N"
        code = f"us{sec.code}{suffix}"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get?param={code},day,,,{limit},qfq"
    else:
        raise KlineError(f"tencent daily unsupported market {sec.market}")
    data = json.loads(_http_text(url))
    payload = (data.get("data") or {}).get(code) or {}
    rows = payload.get("qfqday") or payload.get("day") or []
    bars = [
        _bar(r[0], float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[5]) if len(r) > 5 else 0.0)
        for r in rows
    ]
    if not bars:
        raise KlineError("tencent returned no bars")
    return bars[-limit:]


# Ordered provider chains per (market, period). First success wins.
def _chains_for(market: Market, period: str) -> list[tuple[str, Any]]:
    chain: list[tuple[str, Any]] = [("eastmoney", _eastmoney_klines)]
    if market == Market.CN and period in _SINA_SCALE:
        chain.append(("sina", _sina_cn_klines))
    if period == "1d":
        chain.append(("tencent", _tencent_daily))
    return chain


class KlineService:
    def get_klines(self, secid: str, period: str, limit: int = 200) -> dict[str, Any]:
        sec = parse_secid(secid)
        if sec is None:
            return {"secid": secid, "period": period, "bars": [], "source": "", "error": "invalid secid"}
        errors: list[str] = []
        for name, fn in _chains_for(sec.market, period):
            try:
                bars = fn(secid, period, limit)
                return {"secid": secid, "period": period, "bars": bars, "source": name, "error": None}
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        return {"secid": secid, "period": period, "bars": [], "source": "", "error": "; ".join(errors)}

    def capabilities(self, secid: str) -> dict[str, Any]:
        """Which periods are actually servable right now, probed cheaply.

        We do not probe every period with a full request on each call; the
        frontend disables a period when a real fetch returns an error."""
        sec = parse_secid(secid)
        if sec is None:
            return {"periods": []}
        if sec.market == Market.CN:
            return {"periods": ["5m", "15m", "60m", "1d"], "note": "1m 仅在 Eastmoney K 线服务可达时可用"}
        return {"periods": ["1d", "1m", "5m", "15m", "60m"],
                "note": "美股分钟线依赖 Eastmoney K 线服务；不可达时请求会明确报错并禁用"}
