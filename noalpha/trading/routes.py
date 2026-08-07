from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from noalpha.i18n import DEFAULT_LANG
from noalpha.market.gateway import market_gateway
from noalpha.market.calendar import GlobalMarketSnapshot, global_snapshot, market_status, Market, MARKET_LABEL
from noalpha.trading.autonomous import (
    AutonomousTradingLoop,
    LiveMarketFeed,
    RISK_PROFILES,
    trading_scheduler,
)
from noalpha.trading.continuous_loop import ContinuousIntelligenceLoop
from noalpha.trading.operational_state import AgentOperationalState
from noalpha.trading.proposal import TradeAction, TradeProposal, TradeRationale
from noalpha.trading.session import SessionStatus, TradingSession, TradingStore
from noalpha.trading.virtual_broker import VirtualBroker

logger = logging.getLogger(__name__)


class SessionCreateBody(BaseModel):
    initial_capital: float = Field(default=100000.0, gt=0, le=1e9)
    mode: str = "simulation"  # simulation only; live trading is not implemented
    allowed_markets: list[str] = Field(default_factory=lambda: ["CN", "US"])
    universe: str = "watchlist"
    risk_profile: str = "balanced"
    scan_interval: int = Field(default=300, ge=10, le=86400)
    max_position_weight: float = Field(default=0.10, gt=0, le=0.5)
    max_daily_loss_pct: float = Field(default=0.05, gt=0, le=0.5)
    max_orders_per_day: int = Field(default=20, ge=1, le=200)
    allow_closed_market_fills: bool = False
    lang: str = Field(default=DEFAULT_LANG, max_length=16)


class DebugProposalBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    action: str = "buy"
    target_weight: float = Field(default=0.05, gt=0, le=0.5)
    thesis: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


def register_trading_routes(app: FastAPI, state_dir: Path, admin_dependency: Any | None = None) -> None:
    store = TradingStore(Path(state_dir) / "trading.sqlite3")
    app.state.trading_store = store
    admin_deps = [Depends(admin_dependency)] if admin_dependency else []

    def _universe_provider() -> list[dict[str, Any]]:
        wl_store = getattr(app.state, "market_watchlist_store", None)
        if wl_store is None:
            return []
        return wl_store.list(enabled_only=True)

    def _get_session(session_id: str) -> TradingSession:
        # Prefer ContinuousIntelligenceLoop's session (has live cycle_count)
        cont = _continuous_loops.get(session_id)
        if cont is not None:
            return cont.session
        loop = trading_scheduler.get_loop(session_id)
        if loop is not None:
            return loop.session
        session = store.load_session(session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return session

    def _get_or_restore_loop(session_id: str) -> AutonomousTradingLoop:
        # Prefer the ContinuousIntelligenceLoop — its session object is the
        # one the running thread checks every cycle. _trading_loop is lazily
        # created on the first active-trading phase; if it doesn't exist yet,
        # build one pinned to the CIL's in-memory session so pause/stop
        # mutations land on the object the thread sees.
        cont = _continuous_loops.get(session_id)
        if cont is not None:
            if cont._trading_loop is not None:
                return cont._trading_loop
            # lazy-create trading loop pinned to the CIL's live session
            cont._trading_loop = AutonomousTradingLoop(
                cont.session, store, cont.broker,
                feed=LiveMarketFeed(),
                universe_provider=_universe_provider,
            )
            trading_scheduler.register(cont._trading_loop)
            return cont._trading_loop
        loop = trading_scheduler.get_loop(session_id)
        if loop is not None:
            return loop
        session = store.load_session(session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        state = store.load_broker_state(session_id)
        broker = VirtualBroker.from_state(state) if state else VirtualBroker(
            initial_cash=session.initial_capital, currency=session.currency)
        loop = AutonomousTradingLoop(session, store, broker,
                                     feed=LiveMarketFeed(),
                                     universe_provider=_universe_provider)
        trading_scheduler.register(loop)
        return loop

    # ── sessions ──

    @app.post("/api/v1/trading/sessions", status_code=201, dependencies=admin_deps)
    def create_session(body: SessionCreateBody) -> dict[str, Any]:
        if body.mode != "simulation":
            raise HTTPException(400, "only simulation mode is supported; live trading is intentionally not implemented")
        if body.risk_profile not in RISK_PROFILES:
            raise HTTPException(400, f"risk_profile must be one of {list(RISK_PROFILES)}")
        markets = [m.upper() for m in body.allowed_markets if m.upper() in ("CN", "US")]
        if not markets:
            raise HTTPException(400, "allowed_markets must include CN and/or US")
        currency = "CNY" if markets == ["CN"] else "USD"
        session = TradingSession(
            mode="simulation",
            initial_capital=body.initial_capital,
            current_equity=body.initial_capital,
            cash=body.initial_capital,
            currency=currency,
            allowed_markets=markets,
            universe=body.universe,
            risk_profile=body.risk_profile,
            scan_interval=body.scan_interval,
            max_position_weight=body.max_position_weight,
            max_daily_loss_pct=body.max_daily_loss_pct,
            max_orders_per_day=body.max_orders_per_day,
            allow_closed_market_fills=body.allow_closed_market_fills,
            lang=body.lang,
        )
        store.save_session(session)
        broker = VirtualBroker(initial_cash=body.initial_capital, currency=currency)
        loop = AutonomousTradingLoop(session, store, broker,
                                     feed=LiveMarketFeed(),
                                     universe_provider=_universe_provider)
        trading_scheduler.register(loop)
        return session.to_dict()

    @app.get("/api/v1/trading/sessions")
    def list_sessions() -> dict[str, Any]:
        return {"items": [s.to_dict() for s in store.list_sessions()]}

    @app.get("/api/v1/trading/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        session = _get_session(session_id)
        cont = _continuous_loops.get(session_id) or _get_cont_loop(session_id)
        payload = session.to_dict()
        if cont is not None:
            payload["portfolio"] = cont.broker.get_portfolio_summary()
        else:
            state = store.load_broker_state(session_id)
            if state:
                payload["portfolio"] = VirtualBroker.from_state(state).get_portfolio_summary()
        if cont is not None:
            payload["operational_state"] = cont.op_state.to_dict()
        return payload

    # ── operational state ──

    _continuous_loops: dict[str, ContinuousIntelligenceLoop] = {}
    _cont_threads: dict[str, threading.Thread] = {}

    def _get_cont_loop(session_id: str) -> ContinuousIntelligenceLoop | None:
        if session_id in _continuous_loops:
            return _continuous_loops[session_id]
        session = store.load_session(session_id)
        if session is None:
            return None
        state = store.load_broker_state(session_id)
        broker = VirtualBroker.from_state(state) if state else VirtualBroker(
            initial_cash=session.initial_capital, currency=session.currency)
        loop = ContinuousIntelligenceLoop(
            session, store, broker,
            feed=LiveMarketFeed(),
            universe_provider=_universe_provider,
            home_market=session.allowed_markets[0] if session.allowed_markets else "CN",
            monitored_markets=session.allowed_markets if session.allowed_markets else ["CN", "US", "HK"],
        )
        _continuous_loops[session_id] = loop
        # Kick phase detection immediately so UI never shows "initializing"
        try:
            loop.run_intelligence_cycle()
        except Exception:
            pass
        return loop

    @app.post("/api/v1/trading/sessions/{session_id}/start", dependencies=admin_deps)
    def start_session(session_id: str) -> dict[str, Any]:
        loop = _get_cont_loop(session_id)
        if loop is None:
            raise HTTPException(404, "session not found")
        if loop.session.status == SessionStatus.HALTED:
            raise HTTPException(409, "session is halted (emergency stop); create a new session")
        loop.session.status = SessionStatus.RUNNING
        loop.session.started_at = loop.session.started_at or datetime.now(timezone.utc).isoformat()
        store.save_session(loop.session)

        def _run_cont() -> None:
            import time
            while loop.session.status in (SessionStatus.RUNNING, SessionStatus.PAUSED):
                if loop.session.status == SessionStatus.RUNNING:
                    try:
                        loop.run_intelligence_cycle()
                    except Exception as exc:
                        logger.exception("Intelligence cycle failed")
                for _ in range(int(max(loop._cycle_spacing, 1) * 10)):
                    if loop.session.status not in (SessionStatus.RUNNING, SessionStatus.PAUSED):
                        return
                    if loop.session.status == SessionStatus.PAUSED:
                        time.sleep(0.5)
                        break
                    time.sleep(0.1)

        t = threading.Thread(target=_run_cont, daemon=True, name=f"cont-{session_id}")
        _cont_threads[session_id] = t
        t.start()
        return loop.session.to_dict()

    @app.get("/api/v1/trading/sessions/{session_id}/operational-state")
    def get_operational_state(session_id: str) -> dict[str, Any]:
        loop = _continuous_loops.get(session_id) or _get_cont_loop(session_id)
        if loop is None:
            raise HTTPException(404, "session not loaded")
        return {"session_id": session_id, "operational_state": loop.op_state.to_dict()}

    @app.post("/api/v1/trading/sessions/{session_id}/pause", dependencies=admin_deps)
    def pause_session(session_id: str) -> dict[str, Any]:
        loop = _get_or_restore_loop(session_id)
        loop.session.status = SessionStatus.PAUSED
        store.save_session(loop.session)
        return loop.session.to_dict()

    @app.post("/api/v1/trading/sessions/{session_id}/resume", dependencies=admin_deps)
    def resume_session(session_id: str) -> dict[str, Any]:
        loop = _get_or_restore_loop(session_id)
        if loop.session.status != SessionStatus.PAUSED:
            raise HTTPException(409, f"cannot resume from {loop.session.status.value}")
        trading_scheduler.start(loop)
        return loop.session.to_dict()

    @app.post("/api/v1/trading/sessions/{session_id}/stop", dependencies=admin_deps)
    def stop_session(session_id: str) -> dict[str, Any]:
        loop = _get_or_restore_loop(session_id)
        loop.session.status = SessionStatus.STOPPED
        loop.session.stopped_at = datetime.now(timezone.utc).isoformat()
        store.save_session(loop.session)
        store.save_broker_state(session_id, loop.broker.to_state())
        return loop.session.to_dict()

    @app.post("/api/v1/trading/sessions/{session_id}/emergency-stop", dependencies=admin_deps)
    def emergency_stop(session_id: str) -> dict[str, Any]:
        """HALT: blocks the scheduler loop AND the broker itself, so no new
        proposal can execute even via direct submit paths."""
        loop = _get_or_restore_loop(session_id)
        loop.session.status = SessionStatus.HALTED
        loop.session.stopped_at = datetime.now(timezone.utc).isoformat()
        loop.broker.activate_kill_switch("emergency stop via API")
        store.save_session(loop.session)
        store.save_broker_state(session_id, loop.broker.to_state())
        loop._emit("session", "紧急停止：新提案与新订单已全部阻断",
                   payload={"kill_switch": True})
        return loop.session.to_dict()

    @app.post("/api/v1/trading/sessions/{session_id}/run-cycle", dependencies=admin_deps)
    def run_cycle(session_id: str) -> dict[str, Any]:
        """Run exactly one intelligence cycle synchronously."""
        loop = _get_cont_loop(session_id)
        if loop is None:
            raise HTTPException(404)
        if loop.session.status == SessionStatus.HALTED:
            raise HTTPException(409, "session is halted")
        return loop.run_intelligence_cycle()

    # ── global market snapshot ──

    @app.get("/api/v1/trading/global-market-snapshot")
    def global_market_snapshot() -> dict[str, Any]:
        snap = global_snapshot()
        return {"utc_time": snap.utc_time.isoformat(), "markets": snap.to_dict()}

    # ── daily reports ──

    @app.get("/api/v1/trading/sessions/{session_id}/daily-reports")
    def daily_reports(session_id: str) -> dict[str, Any]:
        _get_session(session_id)
        return {"items": store.list_daily_reports(session_id)}

    @app.get("/api/v1/trading/daily-reports")
    def all_daily_reports() -> dict[str, Any]:
        return {"items": store.list_daily_reports()}

    # ── overnight events ──

    @app.get("/api/v1/trading/sessions/{session_id}/overnight-events")
    def overnight_events(session_id: str) -> dict[str, Any]:
        _get_session(session_id)
        return {"items": store.list_overnight_events(session_id)}

    # ── pending actions ──

    @app.get("/api/v1/trading/sessions/{session_id}/pending-actions")
    def pending_actions(session_id: str) -> dict[str, Any]:
        _get_session(session_id)
        return {"items": store.list_pending_actions(session_id)}

    @app.get("/api/v1/trading/sessions/{session_id}/events")
    def session_events(session_id: str, limit: int = 200, event_type: str | None = None) -> dict[str, Any]:
        _get_session(session_id)
        return {"items": store.list_events(session_id, limit=limit, event_type=event_type)}

    @app.get("/api/v1/trading/sessions/{session_id}/equity-curve")
    def session_equity(session_id: str) -> dict[str, Any]:
        _get_session(session_id)
        return {"items": store.equity_curve(session_id)}

    @app.get("/api/v1/trading/sessions/{session_id}/proposals")
    def session_proposals(session_id: str, limit: int = 50) -> dict[str, Any]:
        """First-class proposals with LIVE status — an executed proposal never
        shows as pending here (unlike the immutable proposal event stream)."""
        _get_session(session_id)
        return {"items": store.list_proposals(session_id, limit=limit)}

    @app.get("/api/v1/trading/sessions/{session_id}/system-truth")
    def session_system_truth(session_id: str) -> dict[str, Any]:
        """Un-fakeable component state: which brains actually participated in
        the last cycle, and the effective decision mode."""
        _get_session(session_id)
        loop = _get_or_restore_loop(session_id)
        # A live loop that has run this process → authoritative.
        if loop.quant_result is not None:
            return loop.system_truth()
        # Otherwise fall back to the last persisted truth snapshot so a
        # restored session reflects its real last cycle, not a blank loop.
        snap = store.list_events(session_id, limit=1, event_type="truth")
        if snap:
            return snap[0]["payload"]
        return loop.system_truth()

    @app.get("/api/v1/trading/sessions/{session_id}/shadow")
    def session_shadow(session_id: str) -> dict[str, Any]:
        """Three-arm attribution.

        `verdict` is the only field a UI may render as a conclusion. The
        numeric deltas are facts about three real counterfactual books, but
        they do not become a claim about incremental LLM alpha until
        `sample.sufficient` is true.
        """
        _get_session(session_id)
        loop = _get_or_restore_loop(session_id)
        report = loop.shadow.attribution()
        report["llm_active"] = loop.trading_research is not None
        if not report["llm_active"]:
            report["verdict"] = "LLM_UNAVAILABLE"
            report["verdict_detail"] = (
                "Arm C ran Arm B's deterministic decisions, so no incremental-LLM "
                "question was asked in this session.")
        return report

    @app.get("/api/v1/trading/sessions/{session_id}/decisions")
    def session_decisions(session_id: str) -> dict[str, Any]:
        """Decision stories: one entry per completed trade (grouped by proposal_id)."""
        _get_session(session_id)
        # Collect all decision events
        dec_events = [e for e in store.list_events(session_id, limit=2000)
                      if e["event_type"] == "decision"]
        # Collect proposal events to merge
        prop_events = [e for e in store.list_events(session_id, limit=2000)
                       if e["event_type"] == "proposal"]
        prop_map: dict[str, dict] = {e["payload"].get("proposal_id", ""): e for e in prop_events}
        # Enrich each decision with proposal payload
        enriched = []
        for de in dec_events:
            pid = de["payload"].get("proposal_id", "")
            prop = prop_map.get(pid)
            de = dict(de)
            de["proposal_payload"] = prop["payload"].get("proposal") if prop else None
            enriched.append(de)
        enriched.sort(key=lambda x: x["timestamp"], reverse=True)
        return {"items": enriched}

    @app.get("/api/v1/trading/sessions/{session_id}/theses")
    def session_theses(session_id: str, symbol: str | None = None) -> dict[str, Any]:
        _get_session(session_id)
        return {"items": store.list_theses(session_id, symbol=symbol)}

    @app.get("/api/v1/trading/sessions/{session_id}/trades/{proposal_id}")
    def trade_detail(session_id: str, proposal_id: str) -> dict[str, Any]:
        """Full decision chain for one trade: candidate → research → proposal
        → risk → order → fill → position → exit."""
        _get_session(session_id)
        events = store.events_for_proposal(session_id, proposal_id)
        if not events:
            raise HTTPException(404, "no events for proposal")
        events.sort(key=lambda e: e["timestamp"])
        return {"proposal_id": proposal_id, "chain": events}

    # ── legacy/aggregate endpoints kept for the dashboard ──

    def _active_loop() -> AutonomousTradingLoop | None:
        sessions = store.list_sessions(limit=10)
        for s in sessions:
            if s.status in (SessionStatus.RUNNING, SessionStatus.PAUSED):
                return _get_or_restore_loop(s.session_id)
        return _get_or_restore_loop(sessions[0].session_id) if sessions else None

    @app.get("/api/v1/trading/portfolio")
    def trading_portfolio() -> dict[str, Any]:
        loop = _active_loop()
        if loop is None:
            return VirtualBroker().get_portfolio_summary() | {"session": None}
        return loop.broker.get_portfolio_summary() | {"session": loop.session.to_dict()}

    @app.get("/api/v1/trading/orders")
    def trading_orders() -> dict[str, Any]:
        loop = _active_loop()
        orders = list(loop.broker.orders.values()) if loop else []
        return {"items": [o.to_dict() for o in orders[-50:]], "total": len(orders)}

    @app.get("/api/v1/trading/fills")
    def trading_fills() -> dict[str, Any]:
        loop = _active_loop()
        fills = loop.broker.fills if loop else []
        return {"items": [f.to_dict() for f in fills[-50:]], "total": len(fills)}

    # ── developer debug: manual proposal (NOT the product path) ──

    @app.post("/api/v1/trading/debug/proposals", dependencies=admin_deps)
    def debug_manual_proposal(body: DebugProposalBody) -> dict[str, Any]:
        """Developer tool only. Price always comes from the market gateway —
        the client can no longer supply its own fill price."""
        loop = _active_loop()
        if loop is None:
            raise HTTPException(409, "no trading session exists; create one first")
        if loop.session.status == SessionStatus.HALTED or loop.broker.kill_switch_active:
            raise HTTPException(409, "session halted / kill switch active")
        quote = market_gateway.get_latest(body.symbol)
        if quote is None:
            secid = market_gateway.resolve_secid(body.symbol)
            if secid:
                fetched = market_gateway.fetch_now([secid])
                quote = fetched[0] if fetched else None
        if quote is None or quote.price <= 0:
            raise HTTPException(422, f"no real market price available for {body.symbol}; refusing to trade")
        qd = quote.to_dict()
        proposal = TradeProposal(
            symbol=body.symbol.upper(), action=TradeAction(body.action),
            target_weight=body.target_weight,
            rationale=TradeRationale(thesis=body.thesis or "manual debug proposal",
                                     confidence=body.confidence),
            confidence=body.confidence,
        )
        loop._secid_by_symbol[proposal.symbol] = qd["secid"]
        summary: dict[str, Any] = {"actions": [], "no_actions": []}
        executed = loop._execute_proposal(proposal, qd, summary)
        store.save_broker_state(loop.session.session_id, loop.broker.to_state())
        return {"proposal": proposal.to_dict(), "executed": executed,
                "risk_result": proposal.risk_result, "summary": summary,
                "price_source": {"source": qd.get("source"), "freshness": qd.get("freshness"),
                                 "timestamp": qd.get("timestamp")}}

    @app.post("/api/v1/trading/kill-switch", dependencies=admin_deps)
    def activate_kill_switch(payload: dict[str, Any] = {}) -> dict[str, Any]:
        loop = _active_loop()
        if loop is None:
            raise HTTPException(409, "no trading session")
        loop.broker.activate_kill_switch(payload.get("reason", "manual"))
        loop.session.status = SessionStatus.HALTED
        store.save_session(loop.session)
        return {"ok": True, "active": True, "session_id": loop.session.session_id}
