"""QuantFeatureEngine: versioned, point-in-time-safe feature computation.

All features are computed with *trailing* windows only (pandas rolling is
causal by default); no centered windows, no full-sample statistics — the
leakage attack suite in tests asserts this by shifting future bars.

A daily bar for date T becomes available after T's close. A feature row at
date T therefore represents "what was knowable at the T-close decision".
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from noalpha.quant.domain import FactorDefinition, FeatureDefinition

_AVAIL = "bar(T) available after T close; row(T) usable for T-close decisions"


def _fd(name: str, version: int, category: str, description: str, formula: str,
        lookback: int, fields: tuple[str, ...] = ("close",)) -> FeatureDefinition:
    return FeatureDefinition(
        name=name, version=version, description=description, formula=formula,
        required_fields=fields, lookback=lookback, availability_rule=_AVAIL,
        category=category,
    )


# ── Feature Registry (第十六/十七条): name:version → definition ──

FEATURE_DEFINITIONS: list[FeatureDefinition] = [
    # A. price / return
    _fd("return_1d", 1, "returns", "1-day close-to-close return", "close[t]/close[t-1]-1", 2),
    _fd("return_5d", 1, "returns", "5-day return", "close[t]/close[t-5]-1", 6),
    _fd("return_10d", 1, "returns", "10-day return", "close[t]/close[t-10]-1", 11),
    _fd("return_20d", 1, "returns", "20-day return", "close[t]/close[t-20]-1", 21),
    _fd("return_60d", 1, "returns", "60-day return", "close[t]/close[t-60]-1", 61),
    # B. momentum
    _fd("momentum_5_20", 1, "momentum", "short vs medium momentum spread",
        "(close[t]/close[t-5]-1) - (close[t]/close[t-20]-1)", 21),
    _fd("momentum_20_60", 1, "momentum", "medium vs long momentum spread",
        "(close[t]/close[t-20]-1) - (close[t]/close[t-60]-1)", 61),
    _fd("distance_from_high", 1, "momentum", "distance below trailing 20d high",
        "close[t]/max(high[t-19..t]) - 1", 20, ("close", "high")),
    _fd("breakout_strength", 1, "momentum", "close vs prior 20d close max, vol-scaled",
        "(close[t]-max(close[t-20..t-1])) / (std(ret_1d,20)*close[t])", 21),
    # C. volatility
    _fd("realized_vol_5d", 1, "volatility", "std of 1d returns, 5d window", "std(ret_1d, 5)", 6),
    _fd("realized_vol_20d", 1, "volatility", "std of 1d returns, 20d window", "std(ret_1d, 20)", 21),
    _fd("downside_vol_20d", 1, "volatility", "std of negative 1d returns, 20d window",
        "std(min(ret_1d,0), 20)", 21),
    _fd("atr_normalized_14", 1, "volatility", "14d average true range / close",
        "mean(TR,14)/close; TR=max(h-l,|h-c1|,|l-c1|)", 15, ("close", "high", "low")),
    # D. volume / liquidity
    _fd("volume_ratio_20", 1, "volume", "today's volume vs trailing 20d mean (excl. today)",
        "volume[t]/mean(volume[t-20..t-1])", 21, ("volume",)),
    _fd("volume_zscore_20", 1, "volume", "volume z-score vs trailing 20d (excl. today)",
        "(volume[t]-mean20)/std20", 21, ("volume",)),
    _fd("amihud_illiq_20", 1, "volume", "Amihud-like illiquidity, 20d mean of |ret|/currency vol",
        "mean(|ret_1d|/(close*volume), 20) * 1e9 ; uses turnover when provider serves it", 21,
        ("close", "volume")),
    # E. trend
    _fd("ma_distance_20", 1, "trend", "close vs 20d moving average", "close[t]/mean(close,20)-1", 20),
    _fd("ma_distance_60", 1, "trend", "close vs 60d moving average", "close[t]/mean(close,60)-1", 60),
    _fd("trend_slope_20", 1, "trend", "OLS slope of log close over 20d, per day",
        "polyfit(log close[t-19..t], deg=1).slope", 20),
    _fd("trend_consistency_20", 1, "trend", "fraction of up days in 20d", "mean(ret_1d>0, 20)", 21),
    # F. mean reversion
    _fd("return_zscore_20", 1, "reversal", "1d return standardized by 20d vol",
        "ret_1d[t]/std(ret_1d,20)", 21),
    _fd("distance_from_mean_20", 1, "reversal", "close deviation from 20d mean in price stds",
        "(close[t]-mean(close,20))/std(close,20)", 20),
    _fd("short_term_reversal_5", 1, "reversal", "negative 5d return (reversal candidate)",
        "-(close[t]/close[t-5]-1)", 6),
]

FEATURE_REGISTRY: dict[str, FeatureDefinition] = {f.feature_id: f for f in FEATURE_DEFINITIONS}

DEFAULT_FEATURE_SET = [f.feature_id for f in FEATURE_DEFINITIONS]
FEATURE_SET_VERSION = "fs-core:v1"

# Factors (第十条): named combinations of features, distinct from features.
FACTOR_DEFINITIONS: list[FactorDefinition] = [
    FactorDefinition("MomentumFactor", 1, "medium-term price momentum",
                     ("return_20d:v1", "momentum_5_20:v1", "distance_from_high:v1")),
    FactorDefinition("ReversalFactor", 1, "short-term mean reversion",
                     ("short_term_reversal_5:v1", "return_zscore_20:v1", "distance_from_mean_20:v1")),
    FactorDefinition("LowVolFactor", 1, "low volatility preference",
                     ("realized_vol_20d:v1", "downside_vol_20d:v1", "atr_normalized_14:v1")),
    FactorDefinition("LiquidityFactor", 1, "volume/liquidity state",
                     ("volume_ratio_20:v1", "volume_zscore_20:v1", "amihud_illiq_20:v1")),
]
FACTOR_REGISTRY: dict[str, FactorDefinition] = {f.factor_id: f for f in FACTOR_DEFINITIONS}


class QuantFeatureEngine:
    """Computes the registered feature set for one instrument's bar history.

    Input bars: ascending list of {time, open, high, low, close, volume, turnover?}.
    Output: DataFrame indexed by date (YYYY-MM-DD) with one column per feature_id.
    Every row t only uses bars <= t (asserted by tests/test_quant_leakage.py).
    """

    def __init__(self, feature_ids: list[str] | None = None) -> None:
        self.feature_ids = list(feature_ids or DEFAULT_FEATURE_SET)
        unknown = [f for f in self.feature_ids if f not in FEATURE_REGISTRY]
        if unknown:
            raise KeyError(f"unknown features: {unknown}")

    @property
    def max_lookback(self) -> int:
        return max(FEATURE_REGISTRY[f].lookback for f in self.feature_ids)

    def compute(self, bars: list[dict]) -> pd.DataFrame:
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars)
        df["date"] = df["time"].astype(str).str[:10]
        df = df.drop_duplicates("date").set_index("date").sort_index()
        close = df["close"].astype(float)
        high = df.get("high", close).astype(float)
        low = df.get("low", close).astype(float)
        volume = df["volume"].astype(float)
        turnover = df["turnover"].astype(float) if "turnover" in df else pd.Series(0.0, index=df.index)
        ret1 = close.pct_change()

        out = pd.DataFrame(index=df.index)
        f = out  # alias
        want = set(self.feature_ids)

        def put(fid: str, series: pd.Series) -> None:
            if fid in want:
                f[fid] = series

        put("return_1d:v1", ret1)
        put("return_5d:v1", close.pct_change(5))
        put("return_10d:v1", close.pct_change(10))
        put("return_20d:v1", close.pct_change(20))
        put("return_60d:v1", close.pct_change(60))
        put("momentum_5_20:v1", close.pct_change(5) - close.pct_change(20))
        put("momentum_20_60:v1", close.pct_change(20) - close.pct_change(60))
        put("distance_from_high:v1", close / high.rolling(20).max() - 1.0)
        if "breakout_strength:v1" in want:
            prior_max = close.shift(1).rolling(20).max()
            denom = ret1.rolling(20).std() * close
            f["breakout_strength:v1"] = (close - prior_max) / denom.replace(0.0, np.nan)
        put("realized_vol_5d:v1", ret1.rolling(5).std())
        put("realized_vol_20d:v1", ret1.rolling(20).std())
        put("downside_vol_20d:v1", ret1.clip(upper=0.0).rolling(20).std())
        if "atr_normalized_14:v1" in want:
            prev_close = close.shift(1)
            tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
            f["atr_normalized_14:v1"] = tr.rolling(14).mean() / close
        if "volume_ratio_20:v1" in want:
            vol_mean20 = volume.shift(1).rolling(20).mean()
            f["volume_ratio_20:v1"] = volume / vol_mean20.replace(0.0, np.nan)
        if "volume_zscore_20:v1" in want:
            m = volume.shift(1).rolling(20).mean()
            s = volume.shift(1).rolling(20).std()
            f["volume_zscore_20:v1"] = (volume - m) / s.replace(0.0, np.nan)
        if "amihud_illiq_20:v1" in want:
            currency_vol = turnover.where(turnover > 0, close * volume)
            f["amihud_illiq_20:v1"] = (ret1.abs() / currency_vol.replace(0.0, np.nan)).rolling(20).mean() * 1e9
        put("ma_distance_20:v1", close / close.rolling(20).mean() - 1.0)
        put("ma_distance_60:v1", close / close.rolling(60).mean() - 1.0)
        if "trend_slope_20:v1" in want:
            logc = np.log(close.replace(0.0, np.nan))
            x = np.arange(20, dtype=float)
            x = x - x.mean()
            denom_x = float((x ** 2).sum())
            f["trend_slope_20:v1"] = logc.rolling(20).apply(
                lambda w: float(np.dot(x, w - w.mean()) / denom_x), raw=True)
        put("trend_consistency_20:v1", (ret1 > 0).astype(float).rolling(20).mean())
        if "return_zscore_20:v1" in want:
            f["return_zscore_20:v1"] = ret1 / ret1.rolling(20).std().replace(0.0, np.nan)
        if "distance_from_mean_20:v1" in want:
            m = close.rolling(20).mean()
            s = close.rolling(20).std()
            f["distance_from_mean_20:v1"] = (close - m) / s.replace(0.0, np.nan)
        put("short_term_reversal_5:v1", -close.pct_change(5))

        return out[[c for c in self.feature_ids if c in out.columns]]

    def compute_latest(self, bars: list[dict]) -> tuple[dict[str, float | None], str]:
        """Feature vector for the most recent bar; returns (values, last_bar_date)."""
        table = self.compute(bars)
        if table.empty:
            return {}, ""
        row = table.iloc[-1]
        values = {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
        return values, str(table.index[-1])
