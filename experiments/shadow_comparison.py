"""Three-arm shadow comparison over real historical bars.

Replays AutonomousTradingLoop day by day through ReplayMarketFeed and reports
the full attribution of the three counterfactual books:

  Arm A quant_only            quant signal alone decides
  Arm B deterministic_hybrid  quant + deterministic research ensemble
  Arm C llm_hybrid            quant + real LLM research fusion

This is the surface that actually answers "does the LLM add incremental
alpha?", so it is deliberately blunt about when it cannot:

  * with no LLM credentials, Arm C falls back to Arm B's decisions and the
    report says LLM_UNAVAILABLE instead of showing a zero delta as a finding;
  * below the sample thresholds the verdict is INSUFFICIENT_SAMPLE, whatever
    the equity deltas happen to look like.

Usage:
  .venv/bin/python experiments/shadow_comparison.py
  .venv/bin/python experiments/shadow_comparison.py --days 60 --out results.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from noalpha.market.kline_service import KlineService
from noalpha.trading.autonomous import AutonomousTradingLoop, ReplayMarketFeed
from noalpha.trading.session import SessionStatus, TradingSession, TradingStore
from noalpha.trading.shadow import ARMS
from noalpha.trading.virtual_broker import VirtualBroker

UNIVERSE = ["1.600519", "0.300750", "0.002463", "105.AAPL", "105.NVDA", "105.MSFT"]


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40, help="trading days to replay")
    ap.add_argument("--bars", type=int, default=200, help="bars to fetch per instrument")
    ap.add_argument("--research-mode", default="deterministic",
                    choices=["deterministic", "llm"])
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    svc = KlineService()
    history: dict[str, list[dict]] = {}
    for secid in UNIVERSE:
        result = svc.get_klines(secid, "1d", args.bars)
        if result["error"]:
            print(f"SKIP {secid}: {result['error'][:90]}")
            continue
        history[secid] = result["bars"]
    if not history:
        print("BLOCKED_BY_EXTERNAL_PROVIDER: no real history available")
        return 1
    print(f"loaded {len(history)} instruments, "
          f"{min(len(b) for b in history.values())}..{max(len(b) for b in history.values())} bars")

    dates = sorted({b["time"][:10] for bars in history.values() for b in bars})
    replay_dates = dates[-args.days:]

    tmp = Path(tempfile.mkdtemp(prefix="shadow_cmp_"))
    session = TradingSession(risk_profile="balanced", allowed_markets=["CN", "US"],
                             allow_closed_market_fills=True,
                             research_mode=args.research_mode)
    session.status = SessionStatus.RUNNING
    store = TradingStore(tmp / "trading.sqlite3")
    broker = VirtualBroker(initial_cash=session.initial_capital)
    feed = ReplayMarketFeed(history)
    universe = [{"secid": s, "symbol": s.split(".")[1],
                 "market": "CN" if s.split(".")[0] in ("0", "1") else "US"}
                for s in history]
    loop = AutonomousTradingLoop(session, store, broker, feed=feed,
                                 universe_provider=lambda: universe)
    # keep the shadow books beside the replay store, never in the shared cache
    loop.shadow.root = tmp
    loop.shadow.deltas_path = tmp / "deltas.jsonl"
    loop.shadow.equity_path = tmp / "equity.jsonl"
    loop.shadow.state_path = tmp / "books.json"

    llm_active = loop.trading_research is not None
    if args.research_mode == "llm" and not llm_active:
        print(f"LLM requested but unavailable: {loop.llm_init_error or 'no API key'}")

    for date in replay_dates:
        feed.set_date(date)
        loop.run_cycle()

    report = loop.shadow.attribution()
    report["replay"] = {
        "instruments": sorted(history),
        "first_date": replay_dates[0], "last_date": replay_dates[-1],
        "cycles_run": len(replay_dates),
        "research_mode": args.research_mode,
        "llm_active": llm_active,
    }
    if not llm_active:
        # Arm C ran Arm B's decisions. A zero delta here is the absence of a
        # measurement, not the measurement of an absence.
        report["verdict"] = "LLM_UNAVAILABLE"
        report["verdict_detail"] = (
            "Arm C fell back to Arm B's deterministic decisions for every cycle, so "
            "no incremental-LLM question was asked and none is answered here.")

    arms = report["portfolio_attribution"]["arms"]
    print()
    print(f"{'arm':<22}{'cum ret':>10}{'max dd':>10}{'turnover':>10}"
          f"{'cost':>10}{'trades':>8}{'sharpe/cyc':>12}")
    print("  " + "─" * 78)
    for arm in ARMS:
        m = arms[arm]
        sharpe = m["sharpe_per_cycle"]
        print(f"  {arm:<20}{_pct(m['cumulative_return']):>10}"
              f"{_pct(m['max_drawdown']):>10}{m['total_turnover']:>10.2f}"
              f"{m['total_cost']:>10.0f}{m['trades']:>8}"
              f"{'n/a' if sharpe is None else f'{sharpe:+.3f}':>12}")

    decision = report["decision_attribution"]
    print()
    print(f"decisions observed      : {decision['decisions_total']}")
    print(f"LLM changed a decision  : {decision['decision_change_count']} "
          f"({decision['decision_change_rate'] * 100:.1f}%)")
    if decision["by_change_type"]:
        print(f"  by type               : {decision['by_change_type']}")
    print(f"vetoes / reduces        : {decision['llm_veto_count']} / "
          f"{decision['llm_reduce_count']}")

    delta_b = report["portfolio_attribution"]["llm_vs_deterministic"]
    delta_a = report["portfolio_attribution"]["llm_vs_quant"]
    print()
    print(f"C − B  return {_pct(delta_b['return_delta'])}   "
          f"drawdown {_pct(delta_b['drawdown_delta'])}   "
          f"turnover {delta_b['turnover_delta']:+.2f}")
    print(f"C − A  return {_pct(delta_a['return_delta'])}   "
          f"drawdown {_pct(delta_a['drawdown_delta'])}   "
          f"turnover {delta_a['turnover_delta']:+.2f}")

    sample = report["sample"]
    print()
    print(f"sample: {sample['cycles']}/{sample['required_cycles']} cycles, "
          f"{sample['decision_changes']}/{sample['required_decision_changes']} "
          f"decision changes")
    print(f"VERDICT: {report['verdict']}")
    if report.get("verdict_detail"):
        print(f"  {report['verdict_detail']}")
    elif report["verdict"] == "INSUFFICIENT_SAMPLE":
        print("  The deltas above are facts about three real books. They are NOT a "
              "claim about incremental alpha until the sample clears both thresholds.")

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"\nsaved {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
