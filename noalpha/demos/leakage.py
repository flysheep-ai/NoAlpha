"""Manufacture a beautiful backtest result, then prove it is a lie.

Run from an installed checkout with::

    noalpha demo leakage

Two identical LightGBM models are trained over identical purged walk-forward
folds on identical real daily bars. The only difference is the label. The
leaky label looks much better by Rank IC, then collapses when both models are
scored on the same returns that could actually have been executed.
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import pandas as pd

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
MIN_INFLATION_FACTOR = 3.0


def executable_returns(history: dict) -> pd.Series:
    """Return what was executable: decide at T close, fill at T+1 open."""
    engine = LabelEngine(LABEL.replace("excess_return", "forward_return"))
    forward = {symbol: engine.forward_return(bars) for symbol, bars in history.items()}
    result = pd.concat(forward, names=["secid", "date"]).swaplevel().sort_index()
    return result[~result.index.duplicated()]


def leaky_label(history: dict) -> pd.Series:
    """close[T+4]/close[T-1]-1: spans T, which the model already sees."""
    series = {}
    for secid, bars in history.items():
        frame = pd.DataFrame(bars)
        frame["date"] = frame["time"].astype(str).str[:10]
        close = (
            frame.drop_duplicates("date")
            .set_index("date")
            .sort_index()["close"]
            .astype(float)
        )
        series[secid] = close.shift(-4) / close.shift(1) - 1
    stacked = pd.concat(series, names=["secid", "date"]).swaplevel().sort_index()
    stacked = stacked[~stacked.index.duplicated()]
    return stacked - stacked.groupby(level="date").transform("mean")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--market", choices=["cn", "us"], default="cn")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Optional market-data cache directory (default: .cache/quant/bars)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Keep the public demonstration focused on its result. These are known
    # third-party migration warnings; application warnings remain visible.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="invalid value encountered in log", category=RuntimeWarning
        )
        warnings.filterwarnings(
            "ignore", message="The argument 'eval_set' is deprecated", module="lightgbm"
        )
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    universe = CN_UNIVERSE if args.market == "cn" else US_UNIVERSE
    cost = CN_COST if args.market == "cn" else US_COST

    print(
        f"fetching {len(universe)} instruments {FETCH_START}..{DATA_END} "
        "(tencent qfq, disk-cached; first run needs network)",
        flush=True,
    )
    data_service = (
        HistoricalDataService(cache_dir=args.cache_dir)
        if args.cache_dir is not None
        else HistoricalDataService()
    )
    history, skipped = data_service.get_universe_history(
        universe, FETCH_START, DATA_END, min_bars=800
    )
    for symbol in skipped:
        print(f"  SKIPPED {symbol}")
    if len(history) < 10:
        print("BLOCKED: fewer than 10 instruments with sufficient real history")
        return 2
    print(f"  usable: {len(history)} instruments\n", flush=True)

    builder = QuantDatasetBuilder(label_id=LABEL)
    splitter = WalkForwardSplitter(
        train_days=500,
        valid_days=60,
        test_days=120,
        step_days=120,
        horizon_days=5,
        embargo_days=5,
    )
    executable = executable_returns(history)
    store = ExperimentStore()

    clean_dataset = builder.build(history, DATA_START, DATA_END)
    print(
        f"walk-forward: {len(list(splitter.split(clean_dataset.dates)))} folds "
        f"(train 500d expanding, valid 60d, test 120d, purge {splitter.purge_days}d)"
    )
    print("training lightgbm twice: identical folds and features, one label swapped\n")

    clean = run_walk_forward(
        clean_dataset,
        "lightgbm",
        splitter=splitter,
        executable_return=executable,
        cost=cost,
        top_k=args.top_k,
        seed=42,
        name=f"{args.market}-lightgbm-CLEAN",
        store=store,
    )

    leaky_dataset = builder.build(history, DATA_START, DATA_END)
    leaky_dataset.frame["label"] = leaky_label(history).reindex(leaky_dataset.frame.index)
    leaky_dataset.frame.dropna(subset=["label"], inplace=True)
    leaky = run_walk_forward(
        leaky_dataset,
        "lightgbm",
        splitter=splitter,
        executable_return=executable,
        cost=cost,
        top_k=args.top_k,
        seed=42,
        name=f"{args.market}-lightgbm-LEAKY",
        store=store,
    )

    for experiment, tag in ((clean, "clean"), (leaky, "leaky")):
        if experiment.status != "done":
            print(f"BLOCKED: {tag} run failed: {experiment.notes}")
            return 2

    clean_signal, leaky_signal = clean.metrics["signal"], leaky.metrics["signal"]
    clean_trading, leaky_trading = clean.metrics["trading"], leaky.metrics["trading"]

    print(f"{'':22s}{'Rank IC':>12s}{'Rank ICIR':>12s}{'net return':>14s}{'sharpe':>10s}")
    print("  " + "─" * 68)
    print(
        f"  {'clean protocol':20s}{clean_signal['rank_ic']:>+12.4f}"
        f"{clean_signal['rank_icir']:>+12.3f}"
        f"{clean_trading['cumulative_net_return']:>+14.2%}"
        f"{clean_trading['sharpe']:>+10.2f}"
    )
    print(
        f"  {'leaky same-bar':20s}{leaky_signal['rank_ic']:>+12.4f}"
        f"{leaky_signal['rank_icir']:>+12.3f}"
        f"{leaky_trading['cumulative_net_return']:>+14.2%}"
        f"{leaky_trading['sharpe']:>+10.2f}"
    )

    inflation = (
        leaky_signal["rank_ic"] / clean_signal["rank_ic"]
        if clean_signal["rank_ic"]
        else float("inf")
    )
    print(f"\n  Rank IC inflation: {inflation:.1f}x")
    print("  The leaky model looks far better in-sample. Scored on returns you could")
    print("  actually have executed, it is worse: the edge was never real.\n")

    if inflation < MIN_INFLATION_FACTOR:
        print(
            f"DEMO FAILED: expected >{MIN_INFLATION_FACTOR}x inflation, "
            f"got {inflation:.1f}x."
        )
        print("Either the label protocol changed or the leak is no longer reproducible.")
        return 1

    print(f"OK: leakage reproduced and rejected ({inflation:.1f}x inflation).")
    return 0
