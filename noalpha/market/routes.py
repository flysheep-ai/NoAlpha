from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from noalpha.market.eastmoney_provider import EastmoneyProvider
from noalpha.market.gateway import market_gateway
from noalpha.i18n import DEFAULT_LANG, is_zh
from noalpha.market.instruments import (
    CN_INDEX_SECIDS, US_INDEX_SECIDS, index_display_name, parse_secid,
)
from noalpha.market.kline_service import PERIODS, KlineService
from noalpha.market.watchlist_store import MarketWatchlistStore


class WatchlistAddBody(BaseModel):
    secid: str = Field(min_length=3, max_length=24)


class WatchlistPatchBody(BaseModel):
    enabled: bool


class WatchlistReorderBody(BaseModel):
    secids: list[str] = Field(default_factory=list, max_length=200)


def register_market_routes(app: FastAPI, db_path: Path, admin_dependency: Any | None = None) -> None:
    provider = EastmoneyProvider()
    klines = KlineService()
    store = MarketWatchlistStore(db_path)
    app.state.market_watchlist_store = store
    admin_deps = [Depends(admin_dependency)] if admin_dependency else []

    @app.get("/api/v1/market/search")
    def market_search(q: str = Query(min_length=1, max_length=32), limit: int = Query(default=10, ge=1, le=30)) -> dict[str, Any]:
        return {"items": provider.search(q, limit=limit), "source": provider.source_name}

    @app.get("/api/v1/market/overview")
    def market_overview(lang: str = DEFAULT_LANG) -> dict[str, Any]:
        quotes = market_gateway.fetch_now(CN_INDEX_SECIDS + US_INDEX_SECIDS)
        zh = is_zh(lang)
        by_secid: dict[str, dict[str, Any]] = {}
        for q in quotes:
            row = q.to_dict()
            # Providers label every index in Chinese, including the US ones.
            row["name"] = index_display_name(row["secid"], zh, fallback=row.get("name", ""))
            by_secid[row["secid"]] = row
        return {
            "cn_indices": [by_secid[s] for s in CN_INDEX_SECIDS if s in by_secid],
            "us_indices": [by_secid[s] for s in US_INDEX_SECIDS if s in by_secid],
            "source": provider.source_name,
        }

    @app.get("/api/v1/market/quote/{secid}")
    def market_quote(secid: str) -> dict[str, Any]:
        if parse_secid(secid) is None:
            raise HTTPException(400, "invalid secid")
        quotes = market_gateway.fetch_now([secid])
        if not quotes:
            raise HTTPException(404, f"no quote for {secid} (provider returned nothing)")
        return quotes[0].to_dict()

    @app.get("/api/v1/market/klines/{secid}")
    def market_klines(secid: str, period: str = Query(default="1d"), limit: int = Query(default=200, ge=10, le=800)) -> dict[str, Any]:
        if parse_secid(secid) is None:
            raise HTTPException(400, "invalid secid")
        if period not in PERIODS and period not in ("30m",):
            raise HTTPException(400, f"unsupported period {period}; supported: {PERIODS}")
        return klines.get_klines(secid, period, limit)

    @app.get("/api/v1/market/kline-capabilities/{secid}")
    def market_kline_caps(secid: str) -> dict[str, Any]:
        return klines.capabilities(secid)

    @app.get("/api/v1/market/movers")
    def market_movers(market: str = Query(default="CN"), rank: str = Query(default="gainers"), limit: int = Query(default=10, ge=1, le=50)) -> dict[str, Any]:
        if market.upper() not in ("CN", "US"):
            raise HTTPException(400, "market must be CN or US")
        if rank not in ("gainers", "losers", "volume", "turnover"):
            raise HTTPException(400, "rank must be gainers|losers|volume|turnover")
        return provider.get_movers(market=market, rank=rank, limit=limit)

    @app.get("/api/v1/market/anomalies")
    def market_anomalies() -> dict[str, Any]:
        return {"items": market_gateway.anomalies(), "coverage": "tracked_universe",
                "note": "覆盖范围为当前追踪的自选与订阅股票，非全市场"}

    # ── watchlist CRUD ──

    @app.get("/api/v1/market/watchlist")
    def watchlist_list() -> dict[str, Any]:
        items = store.list()
        secids = [i["secid"] for i in items]
        if secids:
            market_gateway.fetch_now(secids)
        for item in items:
            quote = market_gateway.get_latest(item["secid"])
            item["quote"] = quote.to_dict() if quote else None
        return {"items": items}

    @app.post("/api/v1/market/watchlist", status_code=201, dependencies=admin_deps)
    def watchlist_add(body: WatchlistAddBody) -> dict[str, Any]:
        sec = parse_secid(body.secid)
        if sec is None:
            raise HTTPException(400, "invalid secid")
        quotes = market_gateway.fetch_now([body.secid])
        if not quotes:
            raise HTTPException(404, f"instrument {body.secid} not found at provider")
        q = quotes[0]
        item = store.add(
            secid=body.secid, symbol=q.instrument_id.symbol, name=q.name,
            market=q.instrument_id.market.value, exchange=q.instrument_id.exchange,
            currency=q.instrument_id.currency,
        )
        market_gateway.subscribe([body.secid])
        return item

    @app.delete("/api/v1/market/watchlist/{secid}", dependencies=admin_deps)
    def watchlist_remove(secid: str) -> dict[str, Any]:
        if not store.remove(secid):
            raise HTTPException(404, "not in watchlist")
        return {"ok": True}

    @app.patch("/api/v1/market/watchlist/{secid}", dependencies=admin_deps)
    def watchlist_patch(secid: str, body: WatchlistPatchBody) -> dict[str, Any]:
        if not store.set_enabled(secid, body.enabled):
            raise HTTPException(404, "not in watchlist")
        return {"ok": True, "enabled": body.enabled}

    @app.post("/api/v1/market/watchlist/reorder", dependencies=admin_deps)
    def watchlist_reorder(body: WatchlistReorderBody) -> dict[str, Any]:
        store.reorder(body.secids)
        return {"ok": True}
