"""Real FX rates (Sina forex feed) for cross-currency portfolio accounting.

A session account has one currency; instruments quoted in another currency
are converted at the live USDCNH rate. The rate, its timestamp and source
are recorded on every conversion — never a hardcoded constant.
"""
from __future__ import annotations

import logging
import time
import urllib.request

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, float, str]] = {}  # pair -> (rate, fetched_epoch, quote_time)
_TTL = 600.0


def _fetch_usdcnh() -> tuple[float, str] | None:
    req = urllib.request.Request(
        "https://hq.sinajs.cn/list=fx_susdcnh",
        headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
        data = raw.split('"')[1].split(",")
        rate = float(data[1])
        quote_time = f"{data[17]} {data[0]}" if len(data) > 17 else data[0]
        if rate <= 0:
            return None
        return rate, quote_time
    except Exception as exc:
        logger.warning("Sina FX fetch failed: %s", exc)
        return None


def get_rate(from_ccy: str, to_ccy: str) -> dict | None:
    """Returns {rate, pair, quote_time, source} or None when unavailable."""
    from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
    if from_ccy == to_ccy:
        return {"rate": 1.0, "pair": f"{from_ccy}{to_ccy}", "quote_time": "", "source": "identity"}
    if {from_ccy, to_ccy} != {"USD", "CNY"}:
        return None
    cached = _CACHE.get("USDCNH")
    if cached and time.time() - cached[1] < _TTL:
        usdcnh, _, quote_time = cached
    else:
        fetched = _fetch_usdcnh()
        if fetched is None:
            if cached:  # stale cache is better than nothing, flagged as such
                usdcnh, _, quote_time = cached
                quote_time += " (stale cache)"
            else:
                return None
        else:
            usdcnh, quote_time = fetched
            _CACHE["USDCNH"] = (usdcnh, time.time(), quote_time)
    rate = usdcnh if (from_ccy, to_ccy) == ("USD", "CNY") else 1.0 / usdcnh
    return {"rate": rate, "pair": f"{from_ccy}{to_ccy}", "quote_time": quote_time, "source": "sina_fx"}
