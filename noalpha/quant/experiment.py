"""Walk-forward experiment runner + reproducible experiment registry.

Every run records dataset version, feature set, label, model params, folds,
seed and metrics to disk (第三十九/四十条). Same inputs + same seed → same
metrics (asserted in tests).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from noalpha.quant.dataset import Fold, QuantDataset, WalkForwardSplitter
from noalpha.quant.domain import QuantExperiment
from noalpha.quant.evaluation import (
    CostModel, equal_weight_benchmark, ic_summary, prediction_metrics, topk_backtest,
)
from noalpha.quant.features import FEATURE_SET_VERSION
from noalpha.quant.labels import LabelEngine
from noalpha.quant.models import QuantModel, create_model

logger = logging.getLogger(__name__)


class ExperimentStore:
    def __init__(self, root: str | Path = ".cache/quant/experiments") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, exp: QuantExperiment) -> Path:
        path = self.root / f"{exp.experiment_id}.json"
        path.write_text(json.dumps(exp.to_dict(), ensure_ascii=False, indent=1, default=str))
        return path

    def load_all(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("exp-*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                continue
        return out


def _slice(dataset: QuantDataset, period: tuple[str, str]) -> tuple[pd.DataFrame, pd.Series]:
    frame = dataset.slice_dates(*period)
    return frame[dataset.feature_columns], frame["label"]


def run_walk_forward(dataset: QuantDataset, model_name: str, *,
                     splitter: WalkForwardSplitter,
                     executable_return: pd.Series,
                     cost: CostModel,
                     params: dict[str, Any] | None = None,
                     top_k: int = 5,
                     seed: int = 42,
                     name: str = "",
                     store: ExperimentStore | None = None) -> QuantExperiment:
    """Trains per fold on train(+valid for early stopping), predicts the test
    window, aggregates signal + trading metrics over all out-of-sample dates.

    executable_return: raw forward returns (open T+1 → open T+1+h) indexed
    like the dataset — used for the trading backtest regardless of what label
    the model was trained on.
    """
    exp = QuantExperiment(
        name=name or f"{model_name}-wf",
        dataset_version=dataset.version.dataset_id,
        feature_set_version=FEATURE_SET_VERSION,
        label_version=dataset.version.label_id,
        model=model_name, parameters=dict(params or {}), seed=seed,
        status="running",
    )
    dates = dataset.dates
    folds = list(splitter.split(dates))
    if not folds:
        exp.status = "failed"
        exp.notes = f"not enough dates ({len(dates)}) for walk-forward"
        if store:
            store.save(exp)
        return exp

    oos_preds: list[pd.Series] = []
    fold_records: list[dict[str, Any]] = []
    importances: dict[str, float] = {}
    for fold in folds:
        x_tr, y_tr = _slice(dataset, fold.train)
        x_va, y_va = _slice(dataset, fold.valid)
        x_te, y_te = _slice(dataset, fold.test)
        if x_tr.empty or x_te.empty:
            continue
        model = create_model(model_name, params, seed=seed)
        model.fit(x_tr, y_tr, x_va, y_va)
        pred = model.predict(x_te)
        oos_preds.append(pred)
        fold_ic = ic_summary(pred, y_te)
        fold_records.append({**fold.to_dict(), "ic": fold_ic})
        for k, v in model.feature_importance().items():
            importances[k] = importances.get(k, 0.0) + float(v)

    if not oos_preds:
        exp.status = "failed"
        exp.notes = "no fold produced predictions"
        if store:
            store.save(exp)
        return exp

    all_pred = pd.concat(oos_preds).sort_index()
    all_pred = all_pred[~all_pred.index.duplicated(keep="first")]
    label = dataset.frame["label"].reindex(all_pred.index)
    exec_ret = executable_return.reindex(all_pred.index)

    horizon = LabelEngine(dataset.version.label_id).definition.horizon_days
    bt = topk_backtest(all_pred, exec_ret, k=top_k, horizon_days=horizon, cost=cost)
    ew = equal_weight_benchmark(exec_ret, horizon_days=horizon, cost=cost)

    exp.folds = fold_records
    exp.train_period = list(folds[0].train)
    exp.test_period = [folds[0].test[0], folds[-1].test[1]]
    total_imp = sum(importances.values()) or 1.0
    exp.metrics = {
        "signal": ic_summary(all_pred, label),
        "prediction": prediction_metrics(all_pred, label),
        "trading": bt.metrics,
        "equal_weight_benchmark": ew.metrics,
        "cost_model": cost.to_dict(),
        "top_k": top_k,
        "n_folds": len(fold_records),
        "oos_rows": int(len(all_pred)),
        "feature_importance": {k: round(v / total_imp, 4)
                               for k, v in sorted(importances.items(), key=lambda kv: -kv[1])},
        "survivorship_bias_risk": dataset.version.survivorship_bias_risk,
    }
    exp.status = "done"
    # non-serialized: out-of-sample predictions for calibration (第五十条)
    exp.oos_predictions = all_pred  # type: ignore[attr-defined]
    if store:
        store.save(exp)
    return exp
