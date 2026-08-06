"""Historical replay of the autonomous trading loop over real daily bars.

Pulls ~120 real daily candles per instrument from the kline provider chain,
then walks the AutonomousTradingLoop forward one trading day at a time via
ReplayMarketFeed (which structurally prevents future leakage). Reports every
BUY / SELL / NO_ACTION with reasons plus the final portfolio.

Run:  .venv/bin/python -m evals.runners.autonomous_replay
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from noalpha.market.kline_service import KlineService
from noalpha.trading.autonomous import AutonomousTradingLoop, ReplayMarketFeed
from noalpha.trading.session import SessionStatus, TradingSession, TradingStore
from noalpha.trading.virtual_broker import VirtualBroker

UNIVERSE = ["1.600519", "0.300750", "0.002463", "105.AAPL", "105.NVDA", "105.MSFT"]
REPLAY_DAYS = 25


def main() -> int:
    svc = KlineService()
    history: dict[str, list[dict]] = {}
    for secid in UNIVERSE:
        result = svc.get_klines(secid, "1d", 160)
        if result["error"]:
            print(f"SKIP {secid}: {result['error'][:90]}")
            continue
        history[secid] = result["bars"]
        print(f"LOADED {secid}: {len(result['bars'])} real bars from {result['source']} "
              f"({result['bars'][0]['time']} → {result['bars'][-1]['time']})")
    if not history:
        print("FATAL: no real history available")
        return 1

    # replay over the last REPLAY_DAYS common trading dates
    all_dates = sorted({b["time"][:10] for bars in history.values() for b in bars})
    replay_dates = all_dates[-REPLAY_DAYS:]
    warmup_end = replay_dates[0]

    tmp = Path(tempfile.mkdtemp(prefix="replay_"))
    store = TradingStore(tmp / "replay.sqlite3")
    session = TradingSession(
        initial_capital=100000.0, allowed_markets=["CN", "US"], currency="USD",
        status=SessionStatus.RUNNING, risk_profile="balanced", universe="replay",
    )
    session.current_equity = session.initial_capital
    session.cash = session.initial_capital
    broker = VirtualBroker(initial_cash=100000.0, currency="USD")
    feed = ReplayMarketFeed(history)
    universe_items = [{"secid": s, "symbol": s.split(".", 1)[1],
                       "market": "CN" if s.split(".")[0] in ("0", "1") else "US"}
                      for s in history]
    loop = AutonomousTradingLoop(session, store, broker, feed=feed,
                                 universe_provider=lambda: universe_items)

    leakage_violations = 0
    for date in replay_dates:
        feed.set_date(date)
        # leak check: nothing visible beyond the current date
        for secid in history:
            for bar in feed.get_daily_bars(secid, 500):
                if bar["time"][:10] > date:
                    leakage_violations += 1
        summary = loop.run_cycle()
        for act in summary["actions"]:
            print(f"{date}  {act['action'].upper():6} {act['symbol']:8} ×{act['quantity']:<8} "
                  f"@ {act['price']:.2f}  equity={summary['equity']:.0f}")

    events = store.list_events(session.session_id, limit=5000)
    counts: dict[str, int] = {}
    for e in events:
        counts[e["event_type"]] = counts.get(e["event_type"], 0) + 1
    buys = [e for e in events if e["event_type"] == "fill" and e["payload"]["fill"]["side"] == "buy"]
    sells = [e for e in events if e["event_type"] == "fill" and e["payload"]["fill"]["side"] == "sell"]
    exits = [e for e in events if e["event_type"] == "exit"]
    no_actions = [e for e in events if e["event_type"] == "no_action"]

    print("\n=== REPLAY RESULT (real historical bars) ===")
    print(f"trading days replayed : {len(replay_dates)} ({replay_dates[0]} → {replay_dates[-1]}, warmup ≤ {warmup_end})")
    print(f"event counts          : {counts}")
    print(f"autonomous BUY fills  : {len(buys)}")
    print(f"autonomous SELL fills : {len(sells)}")
    print(f"exit reasons          : {[e['payload']['exit_reason'] for e in exits]}")
    print(f"NO_ACTION records     : {len(no_actions)} (each with reason)")
    print(f"future_evidence_total : {leakage_violations}")
    print(f"final equity          : {session.current_equity:,.2f}  cash {session.cash:,.2f}")
    print(f"open positions        : {[(s, p.quantity) for s, p in broker.positions.items() if p.quantity > 0]}")
    print(f"cycles persisted      : {session.cycle_count}, equity curve points: {len(store.equity_curve(session.session_id))}")
    print(f"store                 : {tmp}")

    ok = (leakage_violations == 0 and len(buys) > 0 and len(sells) > 0 and len(no_actions) > 0
          and session.cycle_count == len(replay_dates))
    print("\nVERDICT:", "PASS (BUY+SELL+NO_ACTION closed loops, zero leakage)" if ok else "INCOMPLETE — see counts above")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
