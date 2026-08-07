"""Point-in-time dataset builder + walk-forward splitter with purge/embargo.

Rows are (date, secid); date is the *decision date* T. Features use bars <= T
(QuantFeatureEngine is trailing-only); the label uses T+1 open → T+1+h open.
Random shuffled splits are structurally impossible: the only splitter offered
is chronological walk-forward, and train/test are separated by
purge = horizon+1 days plus an embargo (第二十七条).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import pandas as pd

from noalpha.quant.domain import DatasetVersion, content_hash
from noalpha.quant.features import FEATURE_SET_VERSION, QuantFeatureEngine
from noalpha.quant.labels import LabelEngine
from noalpha.quant.provenance import DataSnapshot


@dataclass
class QuantDataset:
    version: DatasetVersion
    frame: pd.DataFrame            # MultiIndex (date, secid); feature cols + "label"
    feature_columns: list[str]
    snapshot: DataSnapshot | None = None   # the raw bars this was built from

    @property
    def dates(self) -> list[str]:
        return sorted(self.frame.index.get_level_values("date").unique())

    def slice_dates(self, start: str, end: str) -> pd.DataFrame:
        dates = self.frame.index.get_level_values("date")
        return self.frame[(dates >= start) & (dates <= end)]


class QuantDatasetBuilder:
    def __init__(self, feature_ids: list[str] | None = None,
                 label_id: str = "excess_return_5d:v1") -> None:
        self.engine = QuantFeatureEngine(feature_ids)
        self.labels = LabelEngine(label_id)

    def build(self, history: dict[str, list[dict[str, Any]]],
              start_date: str, end_date: str,
              min_universe_per_day: int = 5,
              *, provider: str = "tencent-fqkline", adjustment_mode: str = "qfq",
              retrieved_at: str = "") -> QuantDataset:
        """history: secid -> ascending daily bars covering [start-lookback, end+horizon]."""
        feats: dict[str, pd.DataFrame] = {}
        fwd: dict[str, pd.Series] = {}
        for secid, bars in history.items():
            table = self.engine.compute(bars)
            if table.empty:
                continue
            feats[secid] = table
            fwd[secid] = self.labels.forward_return(bars)

        if not feats:
            raise ValueError("no instrument produced features — no real data available")

        stacked_feats = pd.concat(feats, names=["secid", "date"]).swaplevel().sort_index()
        stacked_fwd = pd.concat(fwd, names=["secid", "date"]).swaplevel().sort_index()
        label = self.labels.finalize_cross_sectional(stacked_fwd)

        frame = stacked_feats.copy()
        frame["label"] = label
        dates = frame.index.get_level_values("date")
        frame = frame[(dates >= start_date) & (dates <= end_date)]
        # drop rows with no label (end of history) or all-NaN features
        frame = frame.dropna(subset=["label"])
        frame = frame.dropna(how="all", subset=self.engine.feature_ids)
        # drop dates where the cross-section is too thin for ranking to mean anything
        counts = frame.groupby(level="date").size()
        good_dates = counts[counts >= min_universe_per_day].index
        frame = frame[frame.index.get_level_values("date").isin(good_dates)]

        # The identity of a dataset is the DATA, not its shape. Hashing only
        # {universe, features, label, range, n_rows} meant a provider that
        # silently restated a price produced a byte-identical dataset_id, and
        # two experiments that disagreed could both cite the same dataset.
        snapshot = DataSnapshot.from_history(
            history, provider=provider, adjustment_mode=adjustment_mode,
            start_date=start_date, end_date=end_date, retrieved_at=retrieved_at)

        version = DatasetVersion(
            dataset_id="",
            universe=sorted(history.keys()),
            start_date=start_date, end_date=end_date,
            feature_ids=list(self.engine.feature_ids),
            label_id=self.labels.definition.label_id,
            protocol=self.labels.protocol.to_dict(),
            n_rows=len(frame), n_features=len(self.engine.feature_ids),
            survivorship_bias_risk=True,  # today's watchlist, not PIT constituents (第二十八条)
            raw_data_hash=snapshot.combined_hash,
            data_snapshot=snapshot.to_dict(),
        )
        version.content_hash = content_hash({
            "universe": version.universe, "features": version.feature_ids,
            "label": version.label_id, "range": [start_date, end_date],
            "n_rows": version.n_rows,
            "raw_data": snapshot.combined_hash,
            "provider": snapshot.provider,
            "adjustment_mode": snapshot.adjustment_mode,
        })
        version.dataset_id = f"ds-{FEATURE_SET_VERSION}-{version.content_hash}"
        return QuantDataset(version=version, frame=frame,
                            feature_columns=list(self.engine.feature_ids),
                            snapshot=snapshot)


@dataclass
class Fold:
    fold_id: int
    train: tuple[str, str]
    valid: tuple[str, str]
    test: tuple[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {"fold_id": self.fold_id, "train": list(self.train),
                "valid": list(self.valid), "test": list(self.test)}


@dataclass
class WalkForwardSplitter:
    """Expanding-window walk-forward over trading dates.

    purge_days defaults to horizon+1: a label at decision date T consumes
    prices through T+1+h, so the test window must start at least that many
    trading days after the last train/valid label date. embargo_days adds
    a safety margin on top.
    """
    train_days: int = 500
    valid_days: int = 60
    test_days: int = 120
    step_days: int = 120
    horizon_days: int = 5
    embargo_days: int = 5
    expanding: bool = True

    @property
    def purge_days(self) -> int:
        return self.horizon_days + 1 + self.embargo_days

    def split(self, dates: list[str]) -> Iterator[Fold]:
        n = len(dates)
        fold_id = 0
        start = 0
        while True:
            train_end = (self.train_days if self.expanding else start + self.train_days) - 1
            if not self.expanding:
                train_start = start
            else:
                train_start = 0
                train_end = self.train_days - 1 + start
            valid_start = train_end + 1 + self.purge_days
            valid_end = valid_start + self.valid_days - 1
            test_start = valid_end + 1 + self.purge_days
            test_end = min(test_start + self.test_days - 1, n - 1)
            if test_start >= n:
                return
            yield Fold(
                fold_id=fold_id,
                train=(dates[train_start], dates[train_end]),
                valid=(dates[valid_start], dates[valid_end]),
                test=(dates[test_start], dates[test_end]),
            )
            fold_id += 1
            start += self.step_days
            if test_end >= n - 1:
                return
