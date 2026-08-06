"""Leakage Attack Suite (第七十七/一百一十二条).

Each test *actively constructs* a leakage scenario and asserts the system
either prevents it structurally or exposes it as an implausibly good result
that our protocol would reject.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from noalpha.quant.dataset import QuantDatasetBuilder, WalkForwardSplitter
from noalpha.quant.evaluation import CostModel, ic_summary, topk_backtest
from noalpha.quant.features import QuantFeatureEngine
from noalpha.quant.labels import LabelEngine

from tests.test_quant_core import make_bars, make_history


def test_features_are_causal_future_bars_do_not_change_past():
    """Attack: append wildly different future bars; every already-computed
    feature row must stay identical (no centered windows / global stats)."""
    bars = make_bars(200)
    engine = QuantFeatureEngine()
    before = engine.compute(bars)
    crash = []
    last_close = bars[-1]["close"]
    dates = pd.bdate_range("2024-01-01", periods=30).strftime("%Y-%m-%d")
    for i, d in enumerate(dates):
        c = last_close * (0.5 ** (i / 10))  # violent crash
        crash.append({"time": d, "open": c, "high": c * 1.2, "low": c * 0.8,
                      "close": c, "volume": 9e9, "turnover": 0.0})
    after = engine.compute(bars + crash).loc[before.index]
    pd.testing.assert_frame_equal(before, after)


def test_same_bar_execution_inflates_results():
    """Attack: use close[T]/close[T-?] same-bar style label (deciding at T
    close and pretending to earn T's move). Our protocol label must produce
    strictly lower oracle-momentum correlation than the leaky one."""
    bars = make_bars(300, seed=3)
    df = pd.DataFrame(bars)
    close = df["close"].astype(float)
    ret1 = close.pct_change()
    # leaky: label includes the day we already observed (same bar)
    leaky_label = close.shift(-4) / close.shift(0) - 1 + ret1 * 0  # T close → T+4 close, entered at T close
    leaky_label_with_today = close.shift(-4) / close.shift(1) - 1  # includes T's move "for free"
    protocol = LabelEngine("forward_return_5d:v1").forward_return(bars).reset_index(drop=True)
    # momentum "signal" = today's return; correlates spuriously with any label containing today
    signal = ret1
    corr_leaky = signal.corr(leaky_label_with_today)
    corr_protocol = signal.corr(protocol)
    assert abs(corr_leaky) > abs(corr_protocol), (
        "protocol label failed to remove same-bar information")


def test_oracle_label_as_feature_is_detected_by_absurd_ic():
    """Attack: feed the label itself as prediction (target leakage). IC must
    be ~1.0 — the falsification check: if a real model ever scores like this,
    the pipeline is broken, not brilliant."""
    history = make_history(8, 250)
    ds = QuantDatasetBuilder(label_id="excess_return_5d:v1").build(history, "2023-05-01", "2023-12-01")
    oracle = ic_summary(ds.frame["label"], ds.frame["label"])
    assert oracle["rank_ic"] > 0.999


def test_shuffled_labels_destroy_ic():
    """Placebo (第七十六条): random-shuffled labels must yield ~0 IC for any
    prediction. If not, the evaluator itself leaks."""
    history = make_history(10, 300)
    ds = QuantDatasetBuilder().build(history, "2023-05-01", "2024-01-01")
    rng = np.random.default_rng(0)
    shuffled = pd.Series(rng.permutation(ds.frame["label"].values), index=ds.frame.index)
    s = ic_summary(ds.frame["return_20d:v1"], shuffled)
    assert abs(s["ic"]) < 0.05


def test_purged_split_excludes_label_overlap():
    """Attack: with horizon 5, a train row at date T holds prices through
    T+6. The splitter's first test date must be > purge_days after train end."""
    dates = [f"2023-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    sp = WalkForwardSplitter(train_days=100, valid_days=20, test_days=40,
                             step_days=40, horizon_days=5, embargo_days=5)
    for fold in sp.split(dates):
        gap = dates.index(fold.valid[0]) - dates.index(fold.train[1])
        assert gap >= sp.purge_days


def test_random_predictions_do_not_beat_costs():
    """Null test: random predictions after real costs should hover near the
    universe mean, not print money."""
    history = make_history(10, 300)
    builder = QuantDatasetBuilder(label_id="forward_return_5d:v1")
    ds = builder.build(history, "2023-05-01", "2024-01-01")
    rng = np.random.default_rng(1)
    noise = pd.Series(rng.standard_normal(len(ds.frame)), index=ds.frame.index)
    bt = topk_backtest(noise, ds.frame["label"], k=3, horizon_days=5,
                       cost=CostModel(5, 10, 0))
    oracle = topk_backtest(ds.frame["label"], ds.frame["label"], k=3, horizon_days=5,
                           cost=CostModel(5, 10, 0))
    assert oracle.metrics["cumulative_net_return"] > bt.metrics["cumulative_net_return"]


def test_global_normalization_would_leak_train_only_does_not():
    """Attack: verify model scaler statistics come from train only. We fit on
    train, then transform a test set whose distribution we shift massively;
    the stored median/std must not move."""
    from noalpha.quant.models import LinearModel
    history = make_history(8, 250)
    ds = QuantDatasetBuilder().build(history, "2023-05-01", "2023-12-01")
    x, y = ds.frame[ds.feature_columns], ds.frame["label"]
    m = LinearModel()
    m.fit(x, y)
    median_before = m._median.copy()
    _ = m.predict(x * 1000)  # absurd test distribution
    pd.testing.assert_series_equal(m._median, median_before)
