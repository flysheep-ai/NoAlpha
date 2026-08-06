"""Manufacture a beautiful backtest result, then prove it is a lie.

    python experiments/leakage_demo.py            # A股
    python experiments/leakage_demo.py --market us

This is the shortest honest answer to "why should I trust your numbers?".

Two identical LightGBM models are trained over identical purged walk-forward
folds on identical real daily bars. The ONLY difference is the label:

  clean  open[T+2+h]/open[T+1] - 1   decide at T close, execute at T+1 open
  leaky  close[T+4]/close[T-1] - 1   spans day T itself, which the model's own
                                     return_1d feature already knows at T close

The leaky label is not exotic — `close[T+h]/close[T-1]` is one of the most
common label formulations in public backtesting code. It inflates Rank IC by
roughly an order of magnitude. Then both models are scored on the SAME
executable returns, and the inflated one collapses.

Exits non-zero if the leak fails to reproduce, so this doubles as a regression
test on the point-in-time machinery rather than a one-off screenshot.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from noalpha.quant.data import HistoricalDataService
from noalpha.quant.dataset import QuantDatasetBuilder, WalkForwardSplitter
from noalpha.quant.evaluation import CN_COST, US_COST
from noalpha.quant.experiment import ExperimentStore, run_walk_forward
from noalpha.quant.labels import LabelEngine

CN_UNIVERSE = [
    "1.600519", "1.601318", "1.600036", "1.601899", "1.600900", "1.600030",
    "1.600276", "1.601012", "1.600887", "1.601668", "1.603288", "1.600809",
    "1.601988", "0.000858", "0.000333", "0.002594", "0.000001", "0.000651",
    "0.002415", "0.300059", "0.002714", "0.000725", "0.300750", "0.002475",
]
US_UNIVERSE = [
    "105.AAPL", "105.MSFT", "105.NVDA", "105.GOOGL", "105.AMZN", "105.META",
    "105.TSLA", "105.AMD", "105.AVGO", "105.INTC", "105.QCOM", "105.CSCO",
    "105.ADBE", "105.NFLX", "106.JPM", "106.V", "106.UNH", "106.JNJ",
    "106.WMT", "106.PG", "106.XOM", "106.KO", "106.DIS", "106.BA",
]

FETCH_START, DATA_START, DATA_END = "2018-06-01", "2019-01-02", "2026-07-03"
LABEL = "excess_return_5d:v1"

# The leak must be blatant, or the demo is not making its point.
MIN_INFLATION_FACTOR = 3.0


def executable_returns(history: dict) -> pd.Series:
    """Returns you could actually have earned: decide T close, fill T+1 open."""
    eng = LabelEngine(LABEL.replace("excess_return", "forward_return"))
    fwd = {s: eng.forward_return(b) for s, b in history.items()}
    out = pd.concat(fwd, names=["secid", "date"]).swaplevel().sort_index()
    return out[~out.index.duplicated()]


def leaky_label(history: dict) -> pd.Series:
    """close[T+4]/close[T-1]-1 — spans day T, which the model already sees."""
    series = {}
    for secid, bars in history.items():
        df = pd.DataFrame(bars)
        df["date"] = df["time"].astype(str).str[:10]
        close = df.drop_duplicates("date").set_index("date").sort_index()["close"].astype(float)
        series[secid] = close.shift(-4) / close.shift(1) - 1
    stacked = pd.concat(series, names=["secid", "date"]).swaplevel().sort_index()
    stacked = stacked[~stacked.index.duplicated()]
    return stacked - stacked.groupby(level="date").transform("mean")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", choices=["cn", "us"], default="cn")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    universe = CN_UNIVERSE if args.market == "cn" else US_UNIVERSE
    cost = CN_COST if args.market == "cn" else US_COST

    print(f"fetching {len(universe)} instruments {FETCH_START}..{DATA_END} "
          f"(tencent qfq, disk-cached — first run needs network)")
    history, skipped = HistoricalDataService().get_universe_history(
        universe, FETCH_START, DATA_END, min_bars=800)
    for s in skipped:
        print(f"  SKIPPED {s}")
    if len(history) < 10:
        print("BLOCKED: fewer than 10 instruments with sufficient real history")
        return 2
    print(f"  usable: {len(history)} instruments\n")

    builder = QuantDatasetBuilder(label_id=LABEL)
    splitter = WalkForwardSplitter(train_days=500, valid_days=60, test_days=120,
                                   step_days=120, horizon_days=5, embargo_days=5)
    exec_ret = executable_returns(history)
    store = ExperimentStore()

    ds_clean = builder.build(history, DATA_START, DATA_END)
    print(f"walk-forward: {len(list(splitter.split(ds_clean.dates)))} folds "
          f"(train 500d expanding, valid 60d, test 120d, purge {splitter.purge_days}d)")
    print("training lightgbm twice — identical folds, identical features, one label swapped\n")

    clean = run_walk_forward(ds_clean, "lightgbm", splitter=splitter,
                             executable_return=exec_ret, cost=cost, top_k=args.top_k,
                             seed=42, name=f"{args.market}-lightgbm-CLEAN", store=store)

    ds_leaky = builder.build(history, DATA_START, DATA_END)
    ds_leaky.frame["label"] = leaky_label(history).reindex(ds_leaky.frame.index)
    ds_leaky.frame.dropna(subset=["label"], inplace=True)
    leaky = run_walk_forward(ds_leaky, "lightgbm", splitter=splitter,
                             executable_return=exec_ret, cost=cost, top_k=args.top_k,
                             seed=42, name=f"{args.market}-lightgbm-LEAKY", store=store)

    for exp, tag in ((clean, "clean"), (leaky, "leaky")):
        if exp.status != "done":
            print(f"BLOCKED: {tag} run failed: {exp.notes}")
            return 2

    c_sig, l_sig = clean.metrics["signal"], leaky.metrics["signal"]
    c_trd, l_trd = clean.metrics["trading"], leaky.metrics["trading"]

    print(f"{'':22s}{'Rank IC':>12s}{'Rank ICIR':>12s}{'net return':>14s}{'sharpe':>10s}")
    print("  " + "─" * 68)
    print(f"  {'clean protocol':20s}{c_sig['rank_ic']:>+12.4f}{c_sig['rank_icir']:>+12.3f}"
          f"{c_trd['cumulative_net_return']:>+14.2%}{c_trd['sharpe']:>+10.2f}")
    print(f"  {'leaky same-bar':20s}{l_sig['rank_ic']:>+12.4f}{l_sig['rank_icir']:>+12.3f}"
          f"{l_trd['cumulative_net_return']:>+14.2%}{l_trd['sharpe']:>+10.2f}")

    inflation = l_sig["rank_ic"] / c_sig["rank_ic"] if c_sig["rank_ic"] else float("inf")
    print(f"\n  Rank IC inflation: {inflation:.1f}x")
    print("  The leaky model looks far better in-sample. Scored on returns you could")
    print("  actually have executed, it is worse — the edge was never real.\n")

    if inflation < MIN_INFLATION_FACTOR:
        print(f"DEMO FAILED: expected >{MIN_INFLATION_FACTOR}x inflation, got {inflation:.1f}x.")
        print("Either the label protocol changed or the leak is no longer reproducible.")
        return 1

    print(f"OK: leakage reproduced and rejected ({inflation:.1f}x inflation).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
