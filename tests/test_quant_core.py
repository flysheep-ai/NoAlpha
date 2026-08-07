"""Core quant layer tests: features, labels, splitter, models, evaluation,
registry promotion gate, ensemble conflicts, portfolio construction."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from noalpha.quant.calibration import build_calibration_table, calibrated_confidence
from noalpha.quant.dataset import QuantDatasetBuilder, WalkForwardSplitter
from noalpha.quant.domain import ModelStatus, ModelVersion, QuantSignal, SignalDirection
from noalpha.quant.ensemble import EnsembleInput, SignalEnsemble
from noalpha.quant.evaluation import CostModel, ic_summary, topk_backtest
from noalpha.quant.experiment import ExperimentStore, run_walk_forward
from noalpha.quant.features import FEATURE_REGISTRY, QuantFeatureEngine
from noalpha.quant.labels import LabelEngine
from noalpha.quant.models import create_model
from noalpha.quant.portfolio import EqualWeightOptimizer, InverseVolatilityOptimizer
from noalpha.quant.registry import PromotionError, QuantModelRegistry


def make_bars(n=300, seed=0, drift=0.0005):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(drift, 0.02, n))
    dates = pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d")
    return [
        {"time": d, "open": c * (1 + rng.normal(0, 0.003)), "high": c * 1.01,
         "low": c * 0.99, "close": c, "volume": float(rng.integers(1e5, 1e6)), "turnover": 0.0}
        for d, c in zip(dates, close)
    ]


def make_history(n_secs=8, n=300):
    return {f"105.SYM{i}": make_bars(n, seed=i, drift=0.0002 * (i - 3)) for i in range(n_secs)}


# ── features ──


def test_feature_registry_versioned():
    assert "return_20d:v1" in FEATURE_REGISTRY
    fd = FEATURE_REGISTRY["return_20d:v1"]
    assert fd.formula and fd.lookback == 21 and fd.availability_rule


def test_feature_engine_computes_all():
    engine = QuantFeatureEngine()
    table = engine.compute(make_bars())
    assert set(table.columns) == set(engine.feature_ids)
    # after lookback warmup all features must be finite
    tail = table.iloc[80:]
    assert tail.notna().all().all()


def test_feature_values_match_formulas():
    bars = make_bars()
    table = QuantFeatureEngine().compute(bars)
    closes = [b["close"] for b in bars]
    expect_r5 = closes[-1] / closes[-6] - 1
    assert math.isclose(table["return_5d:v1"].iloc[-1], expect_r5, rel_tol=1e-9)
    ma20 = sum(closes[-20:]) / 20
    assert math.isclose(table["ma_distance_20:v1"].iloc[-1], closes[-1] / ma20 - 1, rel_tol=1e-9)


# ── labels & decision-time protocol ──


def test_label_uses_next_open_execution():
    bars = make_bars(50)
    eng = LabelEngine("forward_return_5d:v1")
    fwd = eng.forward_return(bars)
    opens = [b["open"] for b in bars]
    # label at index t = open[t+6]/open[t+1] - 1
    t = 10
    assert math.isclose(fwd.iloc[t], opens[t + 6] / opens[t + 1] - 1, rel_tol=1e-9)
    # protocol is explicit
    assert eng.protocol.execution_time == "T+1 open"
    # last horizon+1 rows have no label — never fabricated
    assert fwd.iloc[-6:].isna().all()


def test_excess_label_cross_sectional_mean_zero():
    history = make_history()
    ds = QuantDatasetBuilder(label_id="excess_return_5d:v1").build(history, "2023-06-01", "2024-01-01")
    by_date = ds.frame["label"].groupby(level="date").mean()
    assert (by_date.abs() < 1e-9).all()


# ── splitter ──


def test_walk_forward_purge_gap():
    dates = [f"d{i:04d}" for i in range(1000)]
    sp = WalkForwardSplitter(train_days=500, valid_days=60, test_days=120,
                             step_days=120, horizon_days=5, embargo_days=5)
    folds = list(sp.split(dates))
    assert len(folds) >= 2
    for f in folds:
        train_end = dates.index(f.train[1])
        valid_start = dates.index(f.valid[0])
        test_start = dates.index(f.test[0])
        valid_end = dates.index(f.valid[1])
        assert valid_start - train_end > sp.horizon_days + 1  # purged
        assert test_start - valid_end > sp.horizon_days + 1
        assert f.train[1] < f.valid[0] < f.valid[1] < f.test[0]  # chronological


# ── models & evaluation ──


def test_models_fit_predict_deterministic():
    history = make_history()
    ds = QuantDatasetBuilder().build(history, "2023-06-01", "2024-02-01")
    x = ds.frame[ds.feature_columns]
    y = ds.frame["label"]
    for name in ["linear_ridge", "lightgbm", "xgboost", "simple_momentum",
                 "existing_opportunity_score", "random"]:
        m1 = create_model(name, seed=7)
        m1.fit(x, y)
        p1 = m1.predict(x)
        m2 = create_model(name, seed=7)
        m2.fit(x, y)
        p2 = m2.predict(x)
        assert np.allclose(p1.values, p2.values), f"{name} not reproducible"


def test_model_save_load_roundtrip(tmp_path):
    history = make_history()
    ds = QuantDatasetBuilder().build(history, "2023-06-01", "2024-02-01")
    m = create_model("lightgbm")
    m.fit(ds.frame[ds.feature_columns], ds.frame["label"])
    m.save(tmp_path / "m.pkl")
    from noalpha.quant.models import QuantModel
    loaded = QuantModel.load(tmp_path / "m.pkl")
    assert np.allclose(loaded.predict(ds.frame[ds.feature_columns]).values,
                       m.predict(ds.frame[ds.feature_columns]).values)


def test_ic_perfect_and_inverted():
    idx = pd.MultiIndex.from_product([["2024-01-02", "2024-01-03"], list("ABCDE")],
                                     names=["date", "secid"])
    label = pd.Series(np.tile(np.arange(5, dtype=float), 2), index=idx)
    assert ic_summary(label, label)["rank_ic"] == pytest.approx(1.0)
    assert ic_summary(-label, label)["rank_ic"] == pytest.approx(-1.0)


def test_topk_backtest_costs_reduce_returns():
    history = make_history(10, 200)
    ds = QuantDatasetBuilder(label_id="forward_return_5d:v1").build(history, "2023-05-01", "2023-12-01")
    pred = ds.frame["label"]  # oracle predictions: gross should be strongly positive
    free = topk_backtest(pred, ds.frame["label"], k=3, horizon_days=5, cost=CostModel(0, 0, 0))
    costly = topk_backtest(pred, ds.frame["label"], k=3, horizon_days=5,
                           cost=CostModel(50, 50, 50))
    assert costly.metrics["cumulative_net_return"] < free.metrics["cumulative_net_return"]
    assert costly.metrics["total_cost_drag"] > 0


# ── experiment reproducibility ──


def test_walk_forward_experiment_reproducible(tmp_path):
    history = make_history(8, 420)
    builder = QuantDatasetBuilder(label_id="excess_return_5d:v1")
    ds = builder.build(history, "2023-03-01", "2024-08-01")
    raw = builder.labels  # executable returns
    fwd = {s: raw.forward_return(b) for s, b in history.items()}
    exec_ret = pd.concat(fwd, names=["secid", "date"]).swaplevel().sort_index()
    exec_ret = exec_ret[~exec_ret.index.duplicated()]
    sp = WalkForwardSplitter(train_days=150, valid_days=30, test_days=60, step_days=60)
    store = ExperimentStore(tmp_path)
    e1 = run_walk_forward(ds, "lightgbm", splitter=sp, executable_return=exec_ret,
                          cost=CostModel(), seed=11, store=store)
    e2 = run_walk_forward(ds, "lightgbm", splitter=sp, executable_return=exec_ret,
                          cost=CostModel(), seed=11)
    assert e1.status == "done"
    assert e1.metrics["signal"] == e2.metrics["signal"]
    assert (tmp_path / f"{e1.experiment_id}.json").exists()


# ── registry promotion gate ──


def test_promotion_gate_blocks_skipping(tmp_path):
    reg = QuantModelRegistry(tmp_path)
    history = make_history()
    ds = QuantDatasetBuilder().build(history, "2023-06-01", "2024-02-01")
    m = create_model("linear_ridge")
    m.fit(ds.frame[ds.feature_columns], ds.frame["label"])
    v = reg.register(ModelVersion(name="linear_ridge", feature_ids=ds.feature_columns,
                                  label_id="excess_return_5d:v1"), m)
    with pytest.raises(PromotionError):
        reg.promote(v.model_id, ModelStatus.ACTIVE)  # EXPERIMENTAL → ACTIVE forbidden
    # This test asserts the STAGE-ORDER gate, not model quality: it has no
    # evidence and says so through a recorded override rather than by
    # fabricating a bundle that claims checks were run.
    for step in [ModelStatus.VALIDATED, ModelStatus.SHADOW, ModelStatus.PAPER, ModelStatus.ACTIVE]:
        reg.promote(v.model_id, step, override_reason="stage-order test fixture")
    active = reg.active_model()
    assert active is not None and active[0]["model_id"] == v.model_id


# ── ensemble & conflict ──


def _sig(strength, direction=SignalDirection.LONG, calibrated=False, confidence=0.5):
    return QuantSignal(symbol="X", strength=strength, direction=direction,
                       calibrated=calibrated, confidence=confidence)


def test_ensemble_agreement_buys():
    ens = SignalEnsemble()
    d = ens.combine(EnsembleInput(symbol="X", quant=_sig(0.95, calibrated=True, confidence=0.8),
                                  llm_direction="bullish", llm_confidence=0.7))
    assert d.action_hint == "buy" and d.conflict is None


def test_ensemble_conflict_not_averaged():
    ens = SignalEnsemble()
    d = ens.combine(EnsembleInput(symbol="X", quant=_sig(0.95, calibrated=True, confidence=0.85),
                                  llm_direction="bearish", llm_confidence=0.8))
    assert d.conflict is not None
    assert d.action_hint in ("reduce", "avoid")


def test_ensemble_missing_quant_neutral():
    d = SignalEnsemble().combine(EnsembleInput(symbol="X", llm_direction="bullish",
                                               llm_confidence=0.6))
    assert d.components["quant"]["direction"] == "absent"


# ── calibration ──


def test_calibration_monotone_for_perfect_signal():
    idx = pd.MultiIndex.from_product(
        [[f"2024-01-{d:02d}" for d in range(1, 21)], list("ABCDEFGHIJ")],
        names=["date", "secid"])
    ret = pd.Series(np.tile(np.linspace(-0.05, 0.05, 10), 20), index=idx)
    table = build_calibration_table(ret, ret, n_buckets=5)
    hit = [b["hit_rate"] for b in table["buckets"]]
    assert hit[-1] > hit[0]
    conf, ok = calibrated_confidence(0.99, table)
    assert ok and conf > 0.5


# ── portfolio ──


def test_equal_weight_caps_and_turnover_penalty():
    opt = EqualWeightOptimizer(max_weight=0.10, total_exposure=0.9, turnover_penalty=0.5)
    cands = {f"S{i}": {"expected_return": 0.02, "volatility": 0.02} for i in range(4)}
    t = opt.construct(cands, current_weights={})
    assert all(w <= 0.10 + 1e-9 for w in t.weights.values())
    # raw EW 0.25 → capped 0.10 each → penalty 0.5 moves halfway: 0.05 × 4 = 0.20
    assert sum(t.weights.values()) == pytest.approx(0.2, abs=1e-6)


def test_inverse_vol_prefers_low_vol():
    opt = InverseVolatilityOptimizer(max_weight=0.5, turnover_penalty=0.0)
    t = opt.construct({"LOW": {"volatility": 0.01}, "HIGH": {"volatility": 0.04}})
    assert t.weights["LOW"] > t.weights["HIGH"]
