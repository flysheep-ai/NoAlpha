"""Full multi-day replay of the REAL autonomous main path with the ACTIVE
quant model on REAL A-share history (E5 runtime evidence).

Every replay day:
  ReplayMarketFeed (bars <= T only, PIT by construction)
    → quant inference (cross-sectional ranking, refreshed daily)
    → deterministic research on top candidates
    → signal ensemble (+ conflict handling)
    → portfolio optimizer → risk engine → VirtualBroker

Prints real quant-driven BUY / REDUCE chains and a quant attribution summary.
Asserts future leakage = 0 by instrumenting the feed.

Usage: .venv/bin/python experiments/full_replay_phase_q.py [--days 60]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.quant_phase_q import CN_UNIVERSE, DATA_END, FETCH_START
from noalpha.quant.data import HistoricalDataService
from noalpha.trading.autonomous import AutonomousTradingLoop, ReplayMarketFeed
from noalpha.trading.session import SessionStatus, TradingSession, TradingStore
from noalpha.trading.virtual_broker import VirtualBroker


class LeakageGuardFeed(ReplayMarketFeed):
    """Counts any bar served with time > current replay date. Must stay 0."""
    def __init__(self, history):
        super().__init__(history)
        self.future_bars_served = 0

    def get_daily_bars(self, secid, limit=60):
        bars = super().get_daily_bars(secid, limit)
        self.future_bars_served += sum(1 for b in bars if b["time"][:10] > self.current_date)
        return bars


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    svc = HistoricalDataService()
    history, skipped = svc.get_universe_history(CN_UNIVERSE, FETCH_START, DATA_END, min_bars=800)
    print(f"universe: {len(history)} instruments (real cached tencent history), {len(skipped)} skipped")

    all_dates = sorted({b["time"][:10] for bars in history.values() for b in bars})
    replay_dates = all_dates[-args.days:]
    print(f"replaying {len(replay_dates)} trading days: {replay_dates[0]} → {replay_dates[-1]}")

    feed = LeakageGuardFeed(history)
    session = TradingSession(risk_profile="balanced", allowed_markets=["CN"], currency="CNY",
                             initial_capital=1_000_000.0, allow_closed_market_fills=True,
                             universe="phase-q-replay")
    session.status = SessionStatus.RUNNING
    store_path = Path(".cache/quant/replay") / "replay.sqlite3"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    if store_path.exists():
        store_path.unlink()
    store = TradingStore(store_path)
    broker = VirtualBroker(initial_cash=1_000_000.0, currency="CNY")
    universe_items = [{"secid": s, "symbol": s.split(".")[1], "market": "CN"} for s in history]
    loop = AutonomousTradingLoop(session, store, broker, feed=feed,
                                 universe_provider=lambda: universe_items)
    if loop.quant.registry.active_model("CN") is None:
        print("BLOCKED: no ACTIVE CN model in registry — run experiments/train_champion.py first")
        sys.exit(2)

    for d in replay_dates:
        feed.set_date(d)
        loop.run_cycle()

    events = store.list_events(session.session_id, limit=10000)
    by_type = Counter(e["event_type"] for e in events)
    print(f"\nevent counts: {dict(by_type)}")
    assert feed.future_bars_served == 0, f"FUTURE LEAKAGE: {feed.future_bars_served} bars"
    print("future_bars_served = 0 ✓ (asserted)")

    fills = [e for e in events if e["event_type"] == "fill"]
    proposals = {e["payload"]["proposal"]["proposal_id"]: e["payload"]["proposal"]
                 for e in events if e["event_type"] == "proposal"}
    conflicts = [e for e in events if e["event_type"] == "conflict"]
    quant_events = [e for e in events if e["event_type"] == "quant"]

    def show(p, title):
        qv = p["rationale"].get("quant_view") or {}
        print(f"\n── {title}: {p['symbol']} {p['action'].upper()} ──")
        print(f"  quant rank      : {qv.get('rank')}/{qv.get('universe_size')}")
        print(f"  predicted return: {qv.get('predicted_return')}")
        print(f"  probability_up  : {qv.get('probability_up')}")
        print(f"  model           : {qv.get('model') or qv.get('model_id')}")
        twr = p["rationale"].get("target_weight_reason") or {}
        if twr:
            print(f"  weight chain    : alpha {twr.get('quant_alpha_target')} → optimizer "
                  f"{(twr.get('portfolio_optimizer') or {}).get('target')} → pre-risk {twr.get('pre_risk_target')}")
        print(f"  decision_quality: {p.get('decision_quality')} · status {p.get('status')}")

    filled_pids = {e["payload"].get("proposal_id") for e in fills}
    buys = [p for p in proposals.values() if p["action"] == "buy"
            and p["proposal_id"] in filled_pids and p["rationale"].get("quant_view")]
    reduces = [p for p in proposals.values() if p["action"] in ("reduce", "close")
               and p["proposal_id"] in filled_pids and p["rationale"].get("quant_view")]
    if buys:
        show(buys[0], "Quant-driven BUY (real chain)")
    if reduces:
        show(reduces[0], "Quant-driven REDUCE/CLOSE (real chain)")
    if conflicts:
        c = conflicts[0]["payload"]
        print(f"\n── Quant/Research conflict ──\n  {c['quant_view']} vs {c['llm_view']} → {c['resolution']}")

    # quant attribution (第八十九条, first honest version): realized PnL of
    # closed trades grouped by the decision quality of their entry proposal.
    attribution = Counter()
    realized = {}
    for e in events:
        if e["event_type"] == "exit":
            realized[e["payload"]["proposal_id"]] = e["payload"].get("realized_pnl", 0.0)
    for pid, pnl in realized.items():
        q = proposals.get(pid, {}).get("decision_quality", "unknown")
        attribution[q] += pnl
    print(f"\nquant attribution (realized PnL by decision quality): {dict(attribution)}")
    print(f"quant inference refreshes: {len(quant_events)} (one per replay day expected)")
    print(f"final equity: {session.current_equity:,.0f} CNY "
          f"({(session.current_equity / session.initial_capital - 1) * 100:+.2f}%)")

    out = {
        "replay_days": len(replay_dates), "period": [replay_dates[0], replay_dates[-1]],
        "event_counts": dict(by_type), "future_bars_served": feed.future_bars_served,
        "n_quant_buys": len(buys), "n_quant_reduces": len(reduces),
        "n_conflicts": len(conflicts), "fills": len(fills),
        "final_equity": session.current_equity,
        "attribution_realized_pnl": dict(attribution),
    }
    Path("experiments/results/phase_q_replay.json").write_text(json.dumps(out, indent=1))
    print("saved experiments/results/phase_q_replay.json")


if __name__ == "__main__":
    main()
