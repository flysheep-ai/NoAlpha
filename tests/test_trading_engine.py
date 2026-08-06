"""Business-behaviour tests for the autonomous trading engine.

These assert real accounting/decision behaviour (cash conservation, hard
risk rejection, SELL closing the loop, NO_ACTION records, multi-cycle state
retention, zero future leakage) — not just status codes.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from noalpha.trading.autonomous import (
    AutonomousTradingLoop,
    ReplayMarketFeed,
)
from noalpha.trading.domain import Order, OrderSide, OrderType
from noalpha.trading.proposal import TradeAction, TradeProposal, TradeRationale
from noalpha.trading.risk import PreTradeRiskEngine
from noalpha.trading.session import SessionStatus, TradingSession, TradingStore
from noalpha.trading.virtual_broker import VirtualBroker


def make_bars(days: int, start_price: float, daily_ret: float, volume: float = 1_000_000,
              start: dt.date = dt.date(2026, 4, 1), vol_spike_last: float = 1.0,
              last_ret: float | None = None) -> list[dict]:
    bars = []
    price = start_price
    for i in range(days):
        ret = daily_ret if (last_ret is None or i < days - 1) else last_ret
        o = price
        price = price * (1 + ret)
        vol = volume * (vol_spike_last if i == days - 1 else 1.0)
        bars.append({
            "time": (start + dt.timedelta(days=i)).isoformat(),
            "open": o, "high": max(o, price) * 1.004, "low": min(o, price) * 0.996,
            "close": price, "volume": vol, "turnover": price * vol,
        })
    return bars


def build_loop(tmp_path: Path, history: dict[str, list[dict]], **session_kwargs):
    store = TradingStore(tmp_path / "trading.sqlite3")
    session = TradingSession(
        initial_capital=session_kwargs.pop("initial_capital", 100000.0),
        allowed_markets=session_kwargs.pop("allowed_markets", ["US"]),
        currency=session_kwargs.pop("currency", "USD"),
        status=SessionStatus.RUNNING,
        **session_kwargs,
    )
    session.current_equity = session.initial_capital
    session.cash = session.initial_capital
    broker = VirtualBroker(initial_cash=session.initial_capital, currency=session.currency)
    feed = ReplayMarketFeed(history)
    universe = [{"secid": secid, "symbol": secid.split(".", 1)[1], "market": "US"} for secid in history]
    loop = AutonomousTradingLoop(session, store, broker, feed=feed,
                                 universe_provider=lambda: universe)
    return loop, feed, store, broker, session


# ── VirtualBroker accounting ──

class TestBrokerAccounting:
    def test_buy_reduces_cash_and_creates_position(self):
        broker = VirtualBroker(initial_cash=100000)
        order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=100)
        broker.submit_order(order, 200.0)
        assert broker.positions["AAPL"].quantity == 100
        # cash = 100000 - 100*fill_price - commission; fill has slippage
        assert broker.account.cash < 100000 - 100 * 200.0 * 0.999
        assert broker.account.cash == pytest.approx(
            100000 - 100 * (200.0 + 200.0 * 5 / 10000) - 100 * 0.005, rel=1e-9)

    def test_sell_credits_cash(self):
        """The pre-fix bug: SELL proceeds vanished (cash -= then += netting 0)."""
        broker = VirtualBroker(initial_cash=100000)
        broker.submit_order(Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=100), 200.0)
        cash_after_buy = broker.account.cash
        broker.submit_order(Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=100), 210.0)
        assert broker.positions["AAPL"].quantity == 0
        assert broker.account.cash > cash_after_buy + 100 * 209  # proceeds actually landed
        # equity == cash when flat; realized pnl not double counted
        assert broker.account.equity == pytest.approx(broker.account.cash)

    def test_cannot_sell_more_than_held(self):
        broker = VirtualBroker(initial_cash=100000)
        broker.submit_order(Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10), 100.0)
        cash_before = broker.account.cash
        order = broker.submit_order(Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=500), 100.0)
        assert order.quantity == 10  # clamped to holdings
        assert broker.positions["AAPL"].quantity == 0
        assert broker.account.cash == pytest.approx(cash_before + 10 * (100 - 100 * 5 / 10000) - 10 * 0.005)

    def test_sell_without_position_rejected(self):
        broker = VirtualBroker(initial_cash=100000)
        order = broker.submit_order(Order(symbol="TSLA", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=10), 100.0)
        assert order.status.value == "rejected"
        assert broker.account.cash == 100000

    def test_kill_switch_blocks_orders(self):
        broker = VirtualBroker(initial_cash=100000)
        broker.activate_kill_switch("test")
        order = broker.submit_order(Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10), 100.0)
        assert order.status.value == "rejected"
        assert broker.account.cash == 100000

    def test_state_roundtrip(self):
        broker = VirtualBroker(initial_cash=100000)
        broker.submit_order(Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=50), 120.0)
        restored = VirtualBroker.from_state(broker.to_state())
        assert restored.positions["AAPL"].quantity == 50
        assert restored.account.cash == pytest.approx(broker.account.cash)
        assert restored.account.equity == pytest.approx(broker.account.equity)


# ── Risk engine hard enforcement ──

class TestRiskEngine:
    def _proposal(self, weight: float, action=TradeAction.BUY) -> TradeProposal:
        return TradeProposal(symbol="AAPL", action=action, target_weight=weight,
                             rationale=TradeRationale(thesis="t", confidence=0.8), confidence=0.8)

    def test_oversized_buy_adjusted_down_to_max_position(self):
        broker = VirtualBroker(initial_cash=100000)
        engine = PreTradeRiskEngine(max_position_weight=0.05)
        result = engine.check(self._proposal(0.20), broker.account, broker.positions, [], 100.0)
        assert result.approved and result.adjusted
        assert result.adjusted_target_weight == pytest.approx(0.05)

    def test_at_position_limit_rejected(self):
        broker = VirtualBroker(initial_cash=100000)
        broker.submit_order(Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=50), 100.0)
        engine = PreTradeRiskEngine(max_position_weight=0.05)
        result = engine.check(self._proposal(0.05), broker.account, broker.positions, [], 100.0)
        assert not result.approved and result.rejected

    def test_insufficient_cash_rejected(self):
        broker = VirtualBroker(initial_cash=100000)
        broker.account.cash = 10.0
        broker.account.equity = 100000.0
        engine = PreTradeRiskEngine()
        result = engine.check(self._proposal(0.05), broker.account, broker.positions, [], 100.0)
        assert not result.approved
        assert any("cash" in v.lower() for v in result.violations)

    def test_stale_market_data_rejected(self):
        broker = VirtualBroker(initial_cash=100000)
        engine = PreTradeRiskEngine()
        result = engine.check(self._proposal(0.05), broker.account, broker.positions, [], 100.0,
                              market_data_fresh=False)
        assert not result.approved
        assert "stale" in result.reason.lower()

    def test_zero_price_rejected(self):
        broker = VirtualBroker(initial_cash=100000)
        engine = PreTradeRiskEngine()
        result = engine.check(self._proposal(0.05), broker.account, broker.positions, [], 0.0)
        assert not result.approved

    def test_sell_without_position_rejected(self):
        broker = VirtualBroker(initial_cash=100000)
        engine = PreTradeRiskEngine()
        result = engine.check(self._proposal(0.0, TradeAction.CLOSE), broker.account, broker.positions, [], 100.0)
        assert not result.approved


# ── Autonomous loop: BUY / SELL / NO_ACTION / multi-cycle ──

class TestAutonomousLoop:
    def test_autonomous_buy_full_chain(self, tmp_path):
        history = {"105.UPT": make_bars(40, 100, 0.012, vol_spike_last=2.5)}
        loop, feed, store, broker, session = build_loop(tmp_path, history)
        feed.set_date(history["105.UPT"][-1]["time"])
        summary = loop.run_cycle()

        assert summary["actions"], "uptrending stock should be bought"
        action = summary["actions"][0]
        assert action["action"] == "buy" and action["quantity"] > 0
        assert broker.account.cash < session.initial_capital
        assert broker.positions["UPT"].quantity == action["quantity"]
        # full decision chain persisted
        types = [e["event_type"] for e in store.list_events(session.session_id, limit=100)]
        for required in ("scan", "candidate", "research", "agent", "proposal", "risk", "order", "fill", "position", "thesis"):
            assert required in types, f"missing {required} event in decision feed"
        # proposal carries evidence ids and they resolve in the research payload
        proposal_events = store.list_events(session.session_id, event_type="proposal")
        prop = proposal_events[0]["payload"]["proposal"]
        assert prop["rationale"]["supporting_evidence_ids"], "proposal must cite evidence"
        agent_view = prop["rationale"]["agent_view"]
        assert agent_view["framework"] == "TradingAgents-inspired:v1"
        assert {a["role"] for a in agent_view["analysts"]} >= {
            "technical_analyst",
            "quant_analyst",
            "fundamental_analyst",
            "news_analyst",
            "sentiment_analyst",
        }
        assert agent_view["debate"]["bull"]["role"] == "bull_researcher"
        assert agent_view["debate"]["bear"]["role"] == "bear_researcher"
        assert agent_view["trader_plan"]["action_hint"] == "buy"
        assert agent_view["fund_manager"]["hard_risk_engine_required"] is True

    def test_autonomous_sell_on_thesis_invalidation(self, tmp_path):
        up = make_bars(40, 100, 0.012)
        # afterwards the stock crashes for 12 days → momentum broken
        crash = make_bars(12, up[-1]["close"], -0.05,
                          start=dt.date.fromisoformat(up[-1]["time"]) + dt.timedelta(days=1))
        history = {"105.CRSH": up + crash}
        loop, feed, store, broker, session = build_loop(tmp_path, history)

        feed.set_date(up[-1]["time"])
        loop.run_cycle()
        assert broker.positions["CRSH"].quantity > 0, "setup: position must open first"
        qty = broker.positions["CRSH"].quantity
        cash_before = broker.account.cash

        # advance clock into the crash
        feed.set_date(crash[-1]["time"])
        summary = loop.run_cycle()
        sells = [a for a in summary["actions"] if a["action"] in ("close", "sell", "reduce")]
        assert sells, "crash should trigger an autonomous exit"
        assert broker.positions["CRSH"].quantity < qty
        assert broker.account.cash > cash_before, "sell proceeds must land in cash"
        exit_events = store.list_events(session.session_id, event_type="exit")
        assert exit_events, "exit must be recorded with a reason"
        reason = exit_events[0]["payload"]["exit_reason"]
        assert reason in ("stop_loss", "thesis_invalidated", "new_negative_evidence", "take_profit")
        assert exit_events[0]["payload"]["original_thesis"], "exit references the original thesis"

    def test_no_action_recorded_with_reason(self, tmp_path):
        # flat, boring market → no candidate above threshold
        history = {"105.FLAT": make_bars(40, 100, 0.0002)}
        loop, feed, store, broker, session = build_loop(tmp_path, history)
        feed.set_date(history["105.FLAT"][-1]["time"])
        summary = loop.run_cycle()
        assert not summary["actions"]
        assert summary["no_actions"]
        events = store.list_events(session.session_id, event_type="no_action")
        assert events and events[0]["payload"]["reason"]

    def test_multi_cycle_state_retained(self, tmp_path):
        # drift low enough that take-profit (+15%) is not reached mid-test
        bars = make_bars(60, 100, 0.008)
        history = {"105.UPT": bars}
        loop, feed, store, broker, session = build_loop(tmp_path, history)
        equities = []
        for i in (45, 50, 55, 59):
            feed.set_date(bars[i]["time"])
            loop.run_cycle()
            equities.append(session.current_equity)
        assert session.cycle_count == 4
        assert broker.positions["UPT"].quantity > 0
        # position persisted across cycles, not reset
        curve = store.equity_curve(session.session_id)
        assert len(curve) == 4
        # broker state snapshot survives a reload
        restored = VirtualBroker.from_state(store.load_broker_state(session.session_id))
        assert restored.positions["UPT"].quantity == broker.positions["UPT"].quantity

    def test_zero_future_leakage(self, tmp_path):
        bars = make_bars(60, 100, 0.01)
        history = {"105.LEAK": bars}
        feed = ReplayMarketFeed(history)
        cutoff = bars[30]["time"]
        feed.set_date(cutoff)
        visible = feed.get_daily_bars("105.LEAK", limit=100)
        assert all(b["time"] <= cutoff for b in visible)
        quote = feed.get_quote("105.LEAK")
        assert quote["timestamp"] <= cutoff
        future_evidence_total = sum(1 for b in visible if b["time"] > cutoff)
        assert future_evidence_total == 0

    def test_emergency_halt_blocks_execution(self, tmp_path):
        history = {"105.UPT": make_bars(40, 100, 0.012)}
        loop, feed, store, broker, session = build_loop(tmp_path, history)
        feed.set_date(history["105.UPT"][-1]["time"])
        session.status = SessionStatus.HALTED
        broker.activate_kill_switch("test halt")
        result = loop.run_cycle()
        assert result.get("skipped"), "halted session must not run cycles"
        assert not broker.positions

    def test_risk_rejection_no_order(self, tmp_path):
        history = {"105.UPT": make_bars(40, 100, 0.012)}
        loop, feed, store, broker, session = build_loop(tmp_path, history)
        broker.account.cash = 50.0  # essentially no cash
        broker.account.equity = 50.0
        feed.set_date(history["105.UPT"][-1]["time"])
        summary = loop.run_cycle()
        assert not summary["actions"]
        assert not broker.positions
        # risk / no_action recorded
        events = store.list_events(session.session_id)
        assert any(e["event_type"] in ("risk", "no_action") for e in events)

    def test_market_closed_no_buy(self, tmp_path):
        """Live-mode behaviour: candidates above threshold are not traded
        when the market session is closed (allow_closed_market_fills=False)."""
        history = {"105.UPT": make_bars(40, 100, 0.012)}
        loop, feed, store, broker, session = build_loop(tmp_path, history)
        feed.set_date(history["105.UPT"][-1]["time"])
        original_get_quote = feed.get_quote

        def closed_quote(secid):
            q = original_get_quote(secid)
            if q:
                q["freshness"] = "closed"
                q["market_session"] = "closed"
            return q

        feed.get_quote = closed_quote  # type: ignore[assignment]
        summary = loop.run_cycle()
        assert not summary["actions"]
        events = store.list_events(session.session_id, event_type="no_action")
        assert any(e["payload"].get("reason") == "market_not_tradable" for e in events)
