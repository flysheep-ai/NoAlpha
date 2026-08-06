"""LabelEngine: versioned prediction targets with explicit execution assumptions.

Decision-Time Protocol (第二十三条), adopted from Qlib's convention
(qlib/contrib/data/handler.py: Ref($close,-2)/Ref($close,-1)-1) but made
explicit with open prices, matching how this system actually trades:

  decision_time       = T close (the agent decides after seeing T's bar)
  information_cutoff  = bars with date <= T
  execution_time      = T+1 open (first tradable moment after the decision)
  horizon             = h trading days after entry

The forward label at row T is therefore open[T+1+h]/open[T+1] - 1.
Same-bar execution (deciding and filling on T's close) is exactly the bias
found in trading/replay.py and is deliberately impossible here.
"""
from __future__ import annotations

import pandas as pd

from noalpha.quant.domain import DecisionTimeProtocol, LabelDefinition


def _ld(name: str, version: int, kind: str, horizon: int, description: str, formula: str) -> LabelDefinition:
    return LabelDefinition(
        name=name, version=version, description=description, horizon_days=horizon,
        kind=kind, decision_time="T close", execution_time="T+1 open", formula=formula,
    )


LABEL_DEFINITIONS: list[LabelDefinition] = [
    _ld("forward_return_1d", 1, "forward_return", 1,
        "1d forward return, executable", "open[T+2]/open[T+1]-1"),
    _ld("forward_return_5d", 1, "forward_return", 5,
        "5d forward return, executable", "open[T+6]/open[T+1]-1"),
    _ld("forward_return_20d", 1, "forward_return", 20,
        "20d forward return, executable", "open[T+21]/open[T+1]-1"),
    _ld("excess_return_5d", 1, "excess_return", 5,
        "5d forward return minus cross-sectional universe mean (per date)",
        "fwd_5d - mean_universe(fwd_5d)"),
    _ld("cs_rank_5d", 1, "cs_rank", 5,
        "cross-sectional percentile rank of 5d forward return (0..1, 1=best)",
        "rank_pct_universe(fwd_5d)"),
    _ld("direction_5d", 1, "direction", 5, "1 if 5d forward return > 0", "fwd_5d > 0"),
]

LABEL_REGISTRY: dict[str, LabelDefinition] = {ld.label_id: ld for ld in LABEL_DEFINITIONS}


class LabelEngine:
    """Computes per-instrument forward labels; cross-sectional variants are
    finalized by the dataset builder once all instruments are stacked."""

    def __init__(self, label_id: str = "excess_return_5d:v1") -> None:
        if label_id not in LABEL_REGISTRY:
            raise KeyError(f"unknown label {label_id}; known: {list(LABEL_REGISTRY)}")
        self.definition = LABEL_REGISTRY[label_id]
        self.protocol = DecisionTimeProtocol(prediction_horizon_days=self.definition.horizon_days)

    def forward_return(self, bars: list[dict]) -> pd.Series:
        """Executable forward return per decision date T (uses future rows by
        design — this is the *label*, never a feature)."""
        if not bars:
            return pd.Series(dtype=float)
        df = pd.DataFrame(bars)
        df["date"] = df["time"].astype(str).str[:10]
        df = df.drop_duplicates("date").set_index("date").sort_index()
        opens = df["open"].astype(float)
        h = self.definition.horizon_days
        entry = opens.shift(-1)              # T+1 open
        exit_ = opens.shift(-1 - h)          # T+1+h open
        fwd = exit_ / entry - 1.0
        fwd.name = self.definition.label_id
        return fwd

    def finalize_cross_sectional(self, stacked: pd.Series) -> pd.Series:
        """stacked: MultiIndex (date, secid) of raw forward returns."""
        kind = self.definition.kind
        if kind == "forward_return":
            return stacked
        by_date = stacked.groupby(level="date", group_keys=False)
        if kind == "excess_return":
            return stacked - by_date.transform("mean")
        if kind == "cs_rank":
            return by_date.rank(pct=True)
        if kind == "direction":
            return (stacked > 0).astype(float)
        raise ValueError(f"unhandled label kind {kind}")
