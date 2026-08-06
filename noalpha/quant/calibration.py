"""Signal calibration: a model score is not a probability (第四十九/五十条).

We bucket out-of-sample predictions into quantiles and record the empirical
hit rate (label > 0) per bucket. Live signals then map score → historical hit
rate; a signal is only `calibrated=True` when such a table exists.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def build_calibration_table(pred: pd.Series, raw_forward_return: pd.Series,
                            n_buckets: int = 5) -> dict[str, Any]:
    """Reliability table from out-of-sample walk-forward predictions."""
    df = pd.DataFrame({"pred": pred, "ret": raw_forward_return}).dropna()
    if len(df) < n_buckets * 10:
        return {"buckets": [], "n": len(df), "valid": False,
                "note": "insufficient out-of-sample rows for calibration"}
    # cross-sectional quantile per date, then pooled per bucket
    q = df.groupby(level="date")["pred"].rank(pct=True)
    df["bucket"] = np.minimum((q * n_buckets).astype(int), n_buckets - 1)
    buckets = []
    for b, g in df.groupby("bucket"):
        buckets.append({
            "bucket": int(b),
            "score_range": [float(g["pred"].min()), float(g["pred"].max())],
            "hit_rate": float((g["ret"] > 0).mean()),
            "avg_return": float(g["ret"].mean()),
            "n": int(len(g)),
        })
    return {"buckets": buckets, "n": int(len(df)), "n_buckets": n_buckets, "valid": True}


def calibrated_confidence(rank_pct: float, table: dict[str, Any]) -> tuple[float, bool]:
    """Map a live cross-sectional rank percentile (1.0 = best) to the
    historical hit rate of that bucket. Falls back to 0.5 uncalibrated."""
    buckets = table.get("buckets") if table else None
    if not buckets:
        return 0.5, False
    n = len(buckets)
    idx = min(int(rank_pct * n), n - 1)
    return float(buckets[idx]["hit_rate"]), True
