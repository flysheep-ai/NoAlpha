"""Phase Q real-data walk-forward experiments (A股 + 美股).

Usage:
  .venv/bin/python experiments/quant_phase_q.py --market cn
  .venv/bin/python experiments/quant_phase_q.py --market us
  .venv/bin/python experiments/quant_phase_q.py --market cn --leakage-demo

Runs every registered model class over the same purged walk-forward folds on
real Tencent qfq daily history, net of market-specific costs, and stores each
run in the experiment registry. No hyperparameter search is performed —
defaults only — precisely to keep the multiple-testing count low (7 models,
recorded in the summary).

Universe caveat: today's liquid names, not point-in-time index constituents →
every summary carries SURVIVORSHIP_BIAS_RISK=true.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from noalpha.quant.data import HistoricalDataService
from noalpha.quant.dataset import QuantDatasetBuilder, WalkForwardSplitter
from noalpha.quant.evaluation import CN_COST, US_COST, ic_summary
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

MODELS = ["random", "simple_momentum", "simple_reversal", "existing_opportunity_score",
          "linear_ridge", "lightgbm", "xgboost"]

FETCH_START = "2018-06-01"
DATA_START = "2019-01-02"   # leaves ≥60d lookback warmup before this
DATA_END = "2026-07-03"
LABEL = "excess_return_5d:v1"


def executable_returns(history: dict, label_id: str) -> pd.Series:
    eng = LabelEngine(label_id.replace("excess_return", "forward_return"))
    fwd = {s: eng.forward_return(b) for s, b in history.items()}
    out = pd.concat(fwd, names=["secid", "date"]).swaplevel().sort_index()
    return out[~out.index.duplicated()]


def leaky_label(history: dict) -> pd.Series:
    """Deliberately broken label: close[T+4]/close[T-1]-1 — includes day T's
    move, which return_1d already knows at decision time (same-bar leakage)."""
    series = {}
    for secid, bars in history.items():
        df = pd.DataFrame(bars)
        df["date"] = df["time"].astype(str).str[:10]
        close = df.drop_duplicates("date").set_index("date").sort_index()["close"].astype(float)
        series[secid] = close.shift(-4) / close.shift(1) - 1
    stacked = pd.concat(series, names=["secid", "date"]).swaplevel().sort_index()
    stacked = stacked[~stacked.index.duplicated()]
    return stacked - stacked.groupby(level="date").transform("mean")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["cn", "us"], required=True)
    ap.add_argument("--leakage-demo", action="store_true")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    universe = CN_UNIVERSE if args.market == "cn" else US_UNIVERSE
    cost = CN_COST if args.market == "cn" else US_COST
    svc = HistoricalDataService()
    print(f"fetching {len(universe)} instruments {FETCH_START}..{DATA_END} (tencent qfq, cached)")
    history, skipped = svc.get_universe_history(universe, FETCH_START, DATA_END, min_bars=800)
    for s in skipped:
        print(f"  SKIPPED {s}")
    print(f"  usable: {len(history)} instruments")
    if len(history) < 10:
        print("BLOCKED: fewer than 10 instruments with sufficient real history")
        sys.exit(2)

    builder = QuantDatasetBuilder(label_id=LABEL)
    ds = builder.build(history, DATA_START, DATA_END)
    print(f"dataset {ds.version.dataset_id}: {ds.version.n_rows} rows, "
          f"{len(ds.dates)} dates, {ds.version.n_features} features, "
          f"survivorship_bias_risk={ds.version.survivorship_bias_risk}")

    exec_ret = executable_returns(history, LABEL)
    splitter = WalkForwardSplitter(train_days=500, valid_days=60, test_days=120,
                                   step_days=120, horizon_days=5, embargo_days=5)
    n_folds = len(list(splitter.split(ds.dates)))
    print(f"walk-forward: {n_folds} folds "
          f"(train 500d expanding, valid 60d, test 120d, purge {splitter.purge_days}d)")

    store = ExperimentStore()
    results = {}
    for model_name in MODELS:
        exp = run_walk_forward(ds, model_name, splitter=splitter, executable_return=exec_ret,
                               cost=cost, top_k=args.top_k, seed=42,
                               name=f"{args.market}-{model_name}", store=store)
        results[model_name] = exp
        if exp.status != "done":
            print(f"  {model_name:28s} FAILED: {exp.notes}")
            continue
        s, t = exp.metrics["signal"], exp.metrics["trading"]
        print(f"  {model_name:28s} IC {s['ic']:+.4f}  RankIC {s['rank_ic']:+.4f} "
              f"RankICIR {s['rank_icir']:+.3f}  net {t['cumulative_net_return']:+.2%} "
              f"sharpe {t['sharpe']:+.2f}  mdd {t['max_drawdown']:+.2%} "
              f"turn {t['avg_turnover']:.2f}")
    ew = next(e for e in results.values() if e.status == "done").metrics["equal_weight_benchmark"]
    print(f"  {'equal_weight_benchmark':28s} net {ew['cumulative_net_return']:+.2%} "
          f"sharpe {ew['sharpe']:+.2f}  mdd {ew['max_drawdown']:+.2%}")

    if args.leakage_demo:
        print("\n── leakage demo: same-bar label (close[T+4]/close[T-1]) ──")
        leaky = leaky_label(history)
        ds_leaky = builder.build(history, DATA_START, DATA_END)
        ds_leaky.frame["label"] = leaky.reindex(ds_leaky.frame.index)
        ds_leaky.frame.dropna(subset=["label"], inplace=True)
        exp_leaky = run_walk_forward(ds_leaky, "lightgbm", splitter=splitter,
                                     executable_return=exec_ret, cost=cost,
                                     top_k=args.top_k, seed=42,
                                     name=f"{args.market}-lightgbm-LEAKY", store=store)
        clean = results["lightgbm"].metrics["signal"]
        dirty = exp_leaky.metrics["signal"]
        print(f"  clean protocol label : RankIC {clean['rank_ic']:+.4f}")
        print(f"  leaky same-bar label : RankIC {dirty['rank_ic']:+.4f}  ← 虚假优势，必须被拒绝")
        # trading on the *executable* returns exposes the lie:
        print(f"  leaky model traded on executable returns: "
              f"net {exp_leaky.metrics['trading']['cumulative_net_return']:+.2%} "
              f"(vs clean {results['lightgbm'].metrics['trading']['cumulative_net_return']:+.2%})")

    # ── factor screening: per-feature and per-factor IC (no fitting → unbiased) ──
    factor_ic = {}
    for col in ds.feature_columns:
        factor_ic[col] = ic_summary(ds.frame[col], ds.frame["label"])
    from noalpha.quant.features import FACTOR_REGISTRY
    for fid, fdef in FACTOR_REGISTRY.items():
        cols = [c for c in fdef.feature_ids if c in ds.frame.columns]
        if not cols:
            continue
        z = ds.frame[cols].groupby(level="date").rank(pct=True).mean(axis=1)
        factor_ic[fid] = ic_summary(z, ds.frame["label"])

    # ── hybrid comparison (第一百一十五条): same OOS dates, three strategies ──
    hybrid_comparison = {}
    lgbm_exp = results.get("lightgbm")
    if lgbm_exp is not None and lgbm_exp.status == "done":
        import numpy as np
        quant_pred = lgbm_exp.oos_predictions
        f = ds.frame.reindex(quant_pred.index)
        mom5, mom20 = f["return_5d:v1"].fillna(0), f["return_20d:v1"].fillna(0)
        vr, ret1 = f["volume_ratio_20:v1"].fillna(0), f["return_1d:v1"].fillna(0)
        ma20, vol20 = f["ma_distance_20:v1"].fillna(0), f["realized_vol_20d:v1"].fillna(0)
        bulls = ((mom5 > 0.02).astype(int) + (mom20 > 0.05).astype(int)
                 + ((vr >= 2) & (ret1 > 0)).astype(int) + (ma20 >= 0).astype(int))
        bears = ((mom5 < -0.02).astype(int) + (mom20 < -0.05).astype(int)
                 + ((vr >= 2) & (ret1 < 0)).astype(int) + (ma20 < 0).astype(int))
        from noalpha.quant.models import ExistingStrategyBaseline
        opp = ExistingStrategyBaseline()
        conf = (opp.predict(f) * np.where(vol20 > 0.03, 0.85, 1.0)).clip(0.1, 0.9)
        research_score = pd.Series(
            np.where(bulls > bears, 0.5 + conf * 0.5,
                     np.where(bears > bulls, 0.5 - conf * 0.5, 0.5)),
            index=quant_pred.index)
        rank = lambda s: s.groupby(level="date").rank(pct=True)
        hybrid_pred = 0.5625 * rank(quant_pred) + 0.4375 * rank(research_score)
        exec_oos = exec_ret.reindex(quant_pred.index)
        for label_name, p in [("quant_only", quant_pred),
                              ("deterministic_hybrid_baseline", research_score),
                              ("hybrid_ensemble_v1_deprecated", hybrid_pred)]:
            bt = __import__("noalpha.quant.evaluation", fromlist=["topk_backtest"]).topk_backtest(
                p, exec_oos, k=args.top_k, horizon_days=5, cost=cost)
            hybrid_comparison[label_name] = {"signal": ic_summary(p, ds.frame["label"].reindex(p.index)),
                                             "trading": bt.metrics}
            t = bt.metrics
            print(f"  hybrid-cmp {label_name:28s} net {t['cumulative_net_return']:+.2%} "
                  f"sharpe {t['sharpe']:+.2f} turn {t['avg_turnover']:.2f}")

    out = {
        "market": args.market,
        "dataset": ds.version.to_dict(),
        "factor_ic": factor_ic,
        "hybrid_comparison": hybrid_comparison,
        "cost_model": cost.to_dict(),
        "experiment_count": len(MODELS) + (1 if args.leakage_demo else 0),
        "multiple_testing_note": "7 models, no hyperparameter search, defaults only",
        "results": {k: {"experiment_id": v.experiment_id, "status": v.status,
                        "metrics": v.metrics} for k, v in results.items()},
    }
    out_path = Path(f"experiments/results/phase_q_{args.market}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=1, default=str))
    print(f"\nsaved {out_path}")


if __name__ == "__main__":
    main()
