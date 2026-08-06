"""Main-path integration: quant inference inside AutonomousTradingLoop.

Proves with the real loop (real broker, real risk engine, replay feed):
  1. quant-driven BUY — ranking decides research candidates, proposal carries
     real quant_view, decision_quality becomes hybrid;
  2. quant-driven REDUCE — a held position whose rank collapses is reduced
     with exit_reason=rank_deterioration;
  3. quant/LLM conflict — opposing views produce a conflict event and NO trade.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from noalpha.quant.dataset import QuantDatasetBuilder
from noalpha.quant.domain import ModelStatus, ModelVersion
from noalpha.quant.inference import QuantInferenceService
from noalpha.quant.models import create_model
from noalpha.quant.registry import QuantModelRegistry
from noalpha.trading.autonomous import AutonomousTradingLoop, ReplayMarketFeed, ResearchOutcome
from noalpha.trading.session import SessionStatus, TradingSession, TradingStore
from noalpha.trading.virtual_broker import VirtualBroker

from tests.test_quant_core import make_bars


def build_world(tmp_path, n_secs=8, n=260):
    """Synthetic-but-realistic US universe; one symbol (SYM0) crashes hard so
    momentum ranks it last with negative expected return."""
    history = {}
    for i in range(n_secs):
        drift = 0.0 if i == 0 else 0.0006 * (i + 1)
        history[f"105.SYM{i}"] = make_bars(n, seed=i, drift=drift)
    # Shape SYM0 so the *rule* engine holds but the *quant* rank collapses:
    # -3%/day for 10 days, then 10 flat days → mom_20d ≈ -26% (rank last),
    # but mom_5d ≈ 0 and no volume spike, so no rule exit fires.
    level = history["105.SYM0"][-21]["close"]
    for j, b in enumerate(history["105.SYM0"][-20:]):
        if j < 10:
            level *= 0.97
        b["close"] = level
        b["open"] = level * 1.001
        b["high"] = level * 1.005
        b["low"] = level * 0.995
    registry = QuantModelRegistry(tmp_path / "models")
    ds = QuantDatasetBuilder().build(history, "2023-04-01", "2023-12-01")
    model = create_model("simple_momentum")  # deterministic ranking for the test
    model.fit(ds.frame[ds.feature_columns], ds.frame["label"])
    v = registry.register(ModelVersion(name="simple_momentum", feature_ids=ds.feature_columns,
                                       label_id="excess_return_5d:v1", horizon_days=5,
                                       market_scope="US"), model)
    for step in [ModelStatus.VALIDATED, ModelStatus.SHADOW, ModelStatus.PAPER, ModelStatus.ACTIVE]:
        registry.promote(v.model_id, step)

    feed = ReplayMarketFeed(history)
    feed.set_date(history["105.SYM1"][-1]["time"][:10])
    session = TradingSession(risk_profile="aggressive", allowed_markets=["US"],
                             allow_closed_market_fills=True)
    session.status = SessionStatus.RUNNING
    store = TradingStore(tmp_path / "trading.sqlite3")
    broker = VirtualBroker(initial_cash=1_000_000.0)
    universe = [{"secid": s, "symbol": s.split(".")[1], "market": "US"} for s in history]
    loop = AutonomousTradingLoop(session, store, broker, feed=feed,
                                 universe_provider=lambda: universe)
    loop.quant = QuantInferenceService(registry)
    return loop, store, broker, session


def events_of(store, session_id, etype):
    return [e for e in store.list_events(session_id, limit=500) if e["event_type"] == etype]


def test_quant_driven_buy(tmp_path):
    loop, store, broker, session = build_world(tmp_path)
    summary = loop.run_cycle()
    quant_events = events_of(store, session.session_id, "quant")
    assert quant_events, "quant inference event missing"
    payload = quant_events[-1]["payload"]
    assert payload["model"]["display"].startswith("simple_momentum")
    assert payload["top_predictions"][0]["rank"] == 1
    # a BUY executed with hybrid decision quality and real quant evidence
    assert summary["actions"], "no trade executed"
    decisions = events_of(store, session.session_id, "proposal")
    buy_payloads = [d["payload"]["proposal"] for d in decisions
                    if d["payload"]["proposal"]["action"] == "buy"]
    assert buy_payloads
    qv = buy_payloads[0]["rationale"]["quant_view"]
    assert qv["rank"] >= 1 and qv["universe_size"] == 8
    assert qv["model"].startswith("simple_momentum")
    # Provenance is now carried by decision_mode (split out of decision_quality).
    # Quant available + no LLM service = quant_deterministic — the honest truth,
    # NOT a fake "hybrid_llm_quant" label.
    assert buy_payloads[0]["decision_mode"] == "quant_deterministic"
    assert summary["effective_mode"] == "quant_deterministic"
    assert "quant_alpha_target" in buy_payloads[0]["rationale"]["target_weight_reason"]
    # crashing SYM0 must never be the bought one
    assert all(a["symbol"] != "SYM0" for a in summary["actions"] if a["action"] == "buy")


def test_quant_driven_reduce_on_rank_deterioration(tmp_path):
    loop, store, broker, session = build_world(tmp_path)
    # hold the crashing symbol at a mild loss (above stop-loss threshold -12%)
    quote = loop.feed.get_quote("105.SYM0")
    price = quote["price"]
    from noalpha.trading.domain import Order, OrderSide, OrderType
    order = Order(account_id=broker.account.account_id, symbol="SYM0",
                  side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=100)
    broker.submit_order(order, price * 1.05)  # small unrealized loss, no stop-loss
    loop._secid_by_symbol["SYM0"] = "105.SYM0"
    loop.run_cycle()
    reduces = [e for e in store.list_events(session.session_id, limit=500)
               if e["event_type"] == "proposal"
               and e["payload"]["proposal"]["action"] in ("reduce", "close")
               and e["payload"]["proposal"]["symbol"] == "SYM0"
               and e["payload"]["proposal"]["risk_result"].get("exit_reason") == "rank_deterioration"]
    assert reduces, "rank deterioration did not trigger a REDUCE/CLOSE"
    rationale = reduces[0]["payload"]["proposal"]["rationale"]
    assert rationale["quant_view"]["predicted_return"] < 0
    assert "排名恶化" in rationale["thesis"]


def test_quant_llm_conflict_blocks_trade(tmp_path, monkeypatch):
    loop, store, broker, session = build_world(tmp_path)

    def bearish_research(candidate, feed):
        return ResearchOutcome(
            thesis=f"{candidate.symbol} 深度研究：多项负面证据",
            bull_case=[], bear_case=["盈利预警", "行业逆风", "监管风险"],
            risks=["下行风险"], invalidation_conditions=[], confidence=0.8, evidence=[])

    monkeypatch.setattr(loop.engine, "research", bearish_research)
    summary = loop.run_cycle()
    conflicts = events_of(store, session.session_id, "conflict")
    assert conflicts, "conflict event missing"
    detail = conflicts[0]["payload"]
    assert detail["resolution"] in ("no_action", "reduce_position")
    assert not [a for a in summary["actions"] if a["action"] == "buy"], \
        "conflict must not be averaged into a BUY"
