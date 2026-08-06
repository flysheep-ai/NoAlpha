"""Unified QuantModel interface + baselines and ML models.

Preprocessing follows the Qlib protocol (qlib/data/dataset/processor.py):
fill/scale statistics are fit on the TRAIN window only and reused for
valid/test/live — global normalization over the full sample is a leakage
class the attack suite tests for.
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


class QuantModel:
    """fit / predict / save / load. Subclasses set .name."""
    name = "base"
    supports_importance = False

    def __init__(self, params: dict[str, Any] | None = None, seed: int = 42) -> None:
        self.params = dict(params or {})
        self.seed = seed
        self._median: pd.Series | None = None
        self._mean: pd.Series | None = None
        self._std: pd.Series | None = None
        self.feature_columns: list[str] = []

    # ── train-only preprocessing ──

    def _fit_scaler(self, x: pd.DataFrame) -> None:
        self.feature_columns = list(x.columns)
        self._median = x.median()
        self._mean = x.mean()
        std = x.std().replace(0.0, np.nan)
        self._std = std.fillna(1.0)

    def _transform(self, x: pd.DataFrame) -> pd.DataFrame:
        assert self._median is not None, "fit before predict"
        x = x[self.feature_columns].fillna(self._median)
        z = (x - self._mean) / self._std
        return z.clip(-5, 5).fillna(0.0)

    # ── interface ──

    def fit(self, x_train: pd.DataFrame, y_train: pd.Series,
            x_valid: pd.DataFrame | None = None, y_valid: pd.Series | None = None) -> None:
        raise NotImplementedError

    def predict(self, x: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    def feature_importance(self) -> dict[str, float]:
        return {}

    def save(self, path: str | Path) -> None:
        Path(path).write_bytes(pickle.dumps(self))

    @staticmethod
    def load(path: str | Path) -> "QuantModel":
        model = pickle.loads(Path(path).read_bytes())
        if not isinstance(model, QuantModel):
            raise TypeError(f"{path} is not a QuantModel artifact")
        return model


# ── baselines (第二十九条: baseline first) ──


class RandomBaseline(QuantModel):
    name = "random"

    def fit(self, x_train, y_train, x_valid=None, y_valid=None) -> None:
        self._fit_scaler(x_train)

    def predict(self, x: pd.DataFrame) -> pd.Series:
        rng = np.random.default_rng(self.seed)
        return pd.Series(rng.standard_normal(len(x)), index=x.index)


class MomentumBaseline(QuantModel):
    """Prediction = 20d return (simple cross-sectional momentum)."""
    name = "simple_momentum"
    column = "return_20d:v1"

    def fit(self, x_train, y_train, x_valid=None, y_valid=None) -> None:
        self._fit_scaler(x_train)
        if self.column not in x_train.columns:
            raise KeyError(f"{self.name} needs {self.column}")

    def predict(self, x: pd.DataFrame) -> pd.Series:
        return x[self.column].fillna(0.0)


class ReversalBaseline(QuantModel):
    name = "simple_reversal"
    column = "short_term_reversal_5:v1"

    def fit(self, x_train, y_train, x_valid=None, y_valid=None) -> None:
        self._fit_scaler(x_train)

    def predict(self, x: pd.DataFrame) -> pd.Series:
        return x[self.column].fillna(0.0)


class ExistingStrategyBaseline(QuantModel):
    """Replicates trading/autonomous.py opportunity_score on dataset features,
    so the current production heuristic competes in the same arena."""
    name = "existing_opportunity_score"

    def fit(self, x_train, y_train, x_valid=None, y_valid=None) -> None:
        self._fit_scaler(x_train)

    def predict(self, x: pd.DataFrame) -> pd.Series:
        mom5 = x.get("return_5d:v1", pd.Series(0.0, index=x.index)).fillna(0.0)
        mom20 = x.get("return_20d:v1", pd.Series(0.0, index=x.index)).fillna(0.0)
        ma20 = x.get("ma_distance_20:v1", pd.Series(0.0, index=x.index)).fillna(0.0)
        vr = x.get("volume_ratio_20:v1", pd.Series(0.0, index=x.index)).fillna(0.0)
        ret1 = x.get("return_1d:v1", pd.Series(0.0, index=x.index)).fillna(0.0)
        score = (0.5 + (mom5 * 3.0).clip(-0.2, 0.2) + (mom20 * 1.5).clip(-0.15, 0.15)
                 + (ma20 * 2.0).clip(-0.1, 0.1))
        score = score + np.where((vr >= 2.0) & (ret1 > 0), 0.1, 0.0)
        score = score - np.where((vr >= 2.0) & (ret1 < 0), 0.1, 0.0)
        return pd.Series(np.clip(score, 0, 1), index=x.index)


# ── ML models ──


class LinearModel(QuantModel):
    """Ridge regression on z-scored features."""
    name = "linear_ridge"
    supports_importance = True

    def fit(self, x_train, y_train, x_valid=None, y_valid=None) -> None:
        from sklearn.linear_model import Ridge
        self._fit_scaler(x_train)
        self._model = Ridge(alpha=float(self.params.get("alpha", 1.0)), random_state=self.seed)
        self._model.fit(self._transform(x_train), y_train.fillna(0.0))

    def predict(self, x: pd.DataFrame) -> pd.Series:
        return pd.Series(self._model.predict(self._transform(x)), index=x.index)

    def feature_importance(self) -> dict[str, float]:
        return dict(zip(self.feature_columns, np.abs(self._model.coef_).tolist()))


class LightGBMModel(QuantModel):
    """LightGBM regressor, early stopping on the valid fold
    (protocol from qlib/contrib/model/gbdt.py)."""
    name = "lightgbm"
    supports_importance = True

    DEFAULTS = {"learning_rate": 0.05, "num_leaves": 31, "max_depth": -1,
                "min_child_samples": 50, "subsample": 0.9, "subsample_freq": 1,
                "colsample_bytree": 0.9, "reg_lambda": 1.0, "n_estimators": 1000}

    def _lgb_frame(self, x: pd.DataFrame) -> pd.DataFrame:
        # LightGBM forbids JSON special chars (":") in feature names
        z = self._transform(x)
        z.columns = [c.replace(":", "__") for c in z.columns]
        return z

    def fit(self, x_train, y_train, x_valid=None, y_valid=None) -> None:
        import lightgbm as lgb
        self._fit_scaler(x_train)
        params = {**self.DEFAULTS, **self.params, "random_state": self.seed, "verbose": -1}
        self._model = lgb.LGBMRegressor(**params)
        fit_kwargs: dict[str, Any] = {}
        if x_valid is not None and len(x_valid):
            fit_kwargs["eval_set"] = [(self._lgb_frame(x_valid), y_valid.fillna(0.0))]
            fit_kwargs["callbacks"] = [lgb.early_stopping(50, verbose=False)]
        self._model.fit(self._lgb_frame(x_train), y_train.fillna(0.0), **fit_kwargs)

    def predict(self, x: pd.DataFrame) -> pd.Series:
        return pd.Series(self._model.predict(self._lgb_frame(x)), index=x.index)

    def feature_importance(self) -> dict[str, float]:
        return dict(zip(self.feature_columns, self._model.feature_importances_.tolist()))


class XGBoostModel(QuantModel):
    name = "xgboost"
    supports_importance = True

    DEFAULTS = {"learning_rate": 0.05, "max_depth": 5, "min_child_weight": 20,
                "subsample": 0.9, "colsample_bytree": 0.9, "reg_lambda": 1.0,
                "n_estimators": 1000}

    def fit(self, x_train, y_train, x_valid=None, y_valid=None) -> None:
        import xgboost as xgb
        self._fit_scaler(x_train)
        params = {**self.DEFAULTS, **self.params, "random_state": self.seed, "verbosity": 0}
        if x_valid is not None and len(x_valid):
            params["early_stopping_rounds"] = 50
        self._model = xgb.XGBRegressor(**params)
        eval_set = ([(self._transform(x_valid), y_valid.fillna(0.0))]
                    if x_valid is not None and len(x_valid) else None)
        self._model.fit(self._transform(x_train), y_train.fillna(0.0),
                        eval_set=eval_set, verbose=False)

    def predict(self, x: pd.DataFrame) -> pd.Series:
        return pd.Series(self._model.predict(self._transform(x)), index=x.index)

    def feature_importance(self) -> dict[str, float]:
        return dict(zip(self.feature_columns, self._model.feature_importances_.tolist()))


MODEL_CLASSES: dict[str, type[QuantModel]] = {
    cls.name: cls for cls in
    [RandomBaseline, MomentumBaseline, ReversalBaseline, ExistingStrategyBaseline,
     LinearModel, LightGBMModel, XGBoostModel]
}


def create_model(name: str, params: dict[str, Any] | None = None, seed: int = 42) -> QuantModel:
    if name not in MODEL_CLASSES:
        raise KeyError(f"unknown model {name}; known: {list(MODEL_CLASSES)}")
    return MODEL_CLASSES[name](params, seed=seed)
