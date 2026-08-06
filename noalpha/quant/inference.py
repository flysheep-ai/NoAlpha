"""QuantInferenceService: live PIT features → predictions → QuantSignals.

Training/inference parity (第八十一条): the exact same QuantFeatureEngine
version computes features here and in the dataset builder, and the model
artifact carries its own feature column list; a mismatch refuses to predict
rather than silently reindexing.

Feature freshness (第八十二条): if an instrument's last bar is older than
`max_staleness_days` trading days, it gets no prediction — never a stale one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from noalpha.quant.calibration import calibrated_confidence
from noalpha.quant.domain import (
    FeatureSnapshot, ModelVersion, Prediction, QuantSignal, SignalDirection,
)
from noalpha.quant.features import QuantFeatureEngine
from noalpha.quant.registry import QuantModelRegistry

logger = logging.getLogger(__name__)


@dataclass
class InferenceResult:
    as_of: str = ""
    model: dict[str, Any] = field(default_factory=dict)
    predictions: list[Prediction] = field(default_factory=list)
    signals: list[QuantSignal] = field(default_factory=list)
    snapshots: list[FeatureSnapshot] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "model": self.model,
            "predictions": [p.to_dict() for p in self.predictions],
            "signals": [s.to_dict() for s in self.signals],
            "skipped": self.skipped,
            "error": self.error,
        }

    def signal_for(self, symbol: str) -> QuantSignal | None:
        return next((s for s in self.signals if s.symbol == symbol), None)


class QuantInferenceService:
    def __init__(self, registry: QuantModelRegistry, max_staleness_days: int = 5) -> None:
        self.registry = registry
        self.max_staleness_days = max_staleness_days
        self.last_result: InferenceResult | None = None

    def infer(self, universe: list[dict[str, Any]], feed: Any,
              market_scope: str = "ALL", bars_limit: int = 120) -> InferenceResult:
        """universe items: {"secid","symbol",...}; feed: get_daily_bars(secid, limit)."""
        now = datetime.now(timezone.utc).isoformat()
        active = self.registry.active_model(market_scope)
        if active is None:
            return InferenceResult(as_of=now, error="no ACTIVE model in registry")
        record, model = active
        engine = QuantFeatureEngine(record["feature_ids"])
        horizon = int(record.get("horizon_days", 5))

        rows: dict[str, dict[str, float | None]] = {}
        snapshots: list[FeatureSnapshot] = []
        skipped: list[str] = []
        meta: dict[str, dict[str, Any]] = {}
        computed: list[tuple[str, str, dict[str, float | None], int]] = []
        for item in universe:
            secid, symbol = item["secid"], item.get("symbol", item["secid"])
            bars = feed.get_daily_bars(secid, bars_limit)
            if len(bars) < engine.max_lookback + 1:
                skipped.append(f"{secid}: {len(bars)} bars < lookback {engine.max_lookback}")
                continue
            values, last_date = engine.compute_latest(bars)
            computed.append((secid, symbol, values, len(bars)))
            meta[secid] = {"symbol": symbol, "last_date": last_date}
        # Staleness is cross-sectional: an instrument whose last bar lags the
        # freshest bar in the universe by more than max_staleness_days gets no
        # prediction. Works identically for live clock and historical replay.
        freshest = max((meta[s]["last_date"] for s, *_ in computed), default="")
        stale_cutoff = ""
        if freshest:
            stale_cutoff = (datetime.fromisoformat(freshest)
                            - timedelta(days=self.max_staleness_days + 3)).date().isoformat()
        for secid, symbol, values, n_bars in computed:
            last_date = meta[secid]["last_date"]
            stale = bool(stale_cutoff and last_date < stale_cutoff)
            snap = FeatureSnapshot(secid=secid, as_of=now, feature_ids=list(engine.feature_ids),
                                   values=values, bars_used=n_bars, last_bar_time=last_date,
                                   stale=stale)
            snapshots.append(snap)
            if stale:
                skipped.append(f"{secid}: features stale (last bar {last_date} vs universe {freshest})")
                continue
            rows[secid] = values
            meta[secid]["snapshot_id"] = snap.snapshot_id

        result = InferenceResult(as_of=now, snapshots=snapshots, skipped=skipped,
                                 model={"model_id": record["model_id"],
                                        "display": f"{record['name']} v{record['version']}",
                                        "status": record["status"],
                                        "horizon_days": horizon,
                                        "label_id": record.get("label_id", "")})
        if not rows:
            result.error = "no instrument produced fresh features"
            self.last_result = result
            return result

        x = pd.DataFrame.from_dict(rows, orient="index")
        missing = [c for c in model.feature_columns if c not in x.columns]
        if missing:
            result.error = f"training/serving skew: live features missing {missing}"
            self.last_result = result
            return result
        scores = model.predict(x[model.feature_columns])
        ranks = scores.rank(ascending=False, method="first").astype(int)
        pct = scores.rank(pct=True)
        n = len(scores)
        calibration = record.get("calibration") or {}

        for secid in scores.index:
            score, rank = float(scores[secid]), int(ranks[secid])
            conf, calibrated = calibrated_confidence(float(pct[secid]), calibration)
            pred = Prediction(
                instrument_id=secid, symbol=meta[secid]["symbol"],
                model_version=record["model_id"], as_of=now, horizon_days=horizon,
                predicted_return=score, predicted_rank=rank, universe_size=n,
                probability_up=conf if calibrated else None,
                feature_snapshot_id=meta[secid]["snapshot_id"],
            )
            top_third, bottom_third = rank <= max(1, n // 3), rank > n - max(1, n // 3)
            signal = QuantSignal(
                instrument_id=secid, symbol=meta[secid]["symbol"],
                horizon_days=horizon,
                direction=(SignalDirection.LONG if top_third
                           else SignalDirection.SHORT if bottom_third
                           else SignalDirection.NEUTRAL),
                strength=float(pct[secid]),
                confidence=conf, calibrated=calibrated,
                model_version=record["model_id"], prediction_id=pred.prediction_id,
                as_of=now,
            )
            result.predictions.append(pred)
            result.signals.append(signal)
        result.predictions.sort(key=lambda p: p.predicted_rank or 0)
        self.last_result = result
        return result
