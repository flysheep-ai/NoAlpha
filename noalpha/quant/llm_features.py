"""Forward-only PIT LLM feature accumulation (Phase Q5 Goal B).

Every REAL investigation the trading loop runs produces one append-only
LLMFeatureSnapshot. No historical backfill exists anywhere in this module:
a snapshot's available_at is the wall-clock completion time of the actual
LLM call, and any retro-generated record must carry pit_safe=false +
research_only=true and is excluded from dataset-readiness counts.

Why the refusal to backfill, stated precisely
─────────────────────────────────────────────
Guarding the *inputs* to an LLM (see noalpha/pit.py) is not sufficient.
An LLM asked in 2026 what it thought about a stock in 2024 does not reason
from the evidence it was handed — it recalls what actually happened, because
the outcome is in its pretraining corpus. The literature calls this
*parametric look-ahead*, as distinct from input-side leakage:

  - Detecting Lookahead Bias in LLM Forecasts (arXiv:2512.23847) measures it
    directly, showing models recall in-cutoff prices verbatim.
  - Look-Ahead-Bench (arXiv:2601.13770) benchmarks it and finds significant
    temporal bias in general-purpose models.
  - Chronologically-consistent LMs (arXiv:2510.11677) mitigate it by
    retraining on time-stamped corpora — effective, but out of reach for
    most practitioners.

So a retro-generated LLM feature is contaminated no matter how carefully its
inputs were filtered, and no amount of PIT discipline on the evidence side
can repair it. The only clean feature is one generated before the outcome
existed. That is why this store accumulates forward and reports COLLECTING
until it has enough matured rows, instead of manufacturing history on demand.

The cost is honest and large: readiness is measured in months, not minutes.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LLM_FEATURE_SCHEMA_VERSION = "llm_feature_schema:v1"

# feature columns a future model may train on (numeric, semantics in
# research_fusion.TradingResearchSnapshot docstring)
FEATURE_COLUMNS = [
    "bull_strength", "bear_strength", "thesis_confidence", "evidence_quality",
    "citation_support", "event_materiality", "positive_catalyst_score",
    "negative_risk_score", "contradiction_score", "unknown_risk_score",
]


@dataclass
class LLMFeatureSnapshot:
    feature_snapshot_id: str = ""
    schema_version: str = LLM_FEATURE_SCHEMA_VERSION
    instrument_id: str = ""
    symbol: str = ""
    market: str = ""
    as_of: str = ""
    generated_at: str = ""
    available_at: str = ""            # training may only use available_at <= decision_time
    mature_at: str = ""               # label join allowed only after this
    horizon_days: int = 5
    investigation_id: str = ""
    model: str = ""
    model_provider: str = ""
    prompt_versions: dict[str, str] = field(default_factory=dict)
    context_id: str = ""
    evidence_cutoff: str = ""
    features: dict[str, float] = field(default_factory=dict)
    thesis_direction: str = "neutral"
    expected_horizon: str = ""
    pit_safe: bool = True
    research_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LLMFeatureStore:
    """Append-only JSONL store. Snapshots are never updated in place —
    the same instrument at 10:00 and 14:00 yields two rows (第三十条)."""

    def __init__(self, root: str | Path = ".cache/quant/llm_features") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "snapshots.jsonl"

    def append(self, snap: LLMFeatureSnapshot) -> None:
        if not snap.feature_snapshot_id:
            raise ValueError("snapshot needs an id")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap.to_dict(), ensure_ascii=False) + "\n")

    def append_from_trading_snapshot(self, trs: Any, horizon_days: int = 5) -> LLMFeatureSnapshot:
        generated = datetime.fromisoformat(trs.generated_at)
        snap = LLMFeatureSnapshot(
            feature_snapshot_id=f"lfs-{trs.snapshot_id}",
            instrument_id=trs.instrument_id, symbol=trs.symbol, market=trs.market,
            as_of=trs.as_of, generated_at=trs.generated_at,
            available_at=trs.available_at,
            # outcome for a 5D horizon matures ~horizon trading days later;
            # calendar approximation horizon*1.5 days, refined at join time
            mature_at=(generated + timedelta(days=round(horizon_days * 1.5) + 1)).isoformat(),
            horizon_days=horizon_days,
            investigation_id=trs.investigation_id,
            model=trs.model, model_provider=trs.llm_provider,
            prompt_versions=dict(trs.prompt_versions), context_id=trs.context_id,
            evidence_cutoff=trs.as_of,
            features={k: float(getattr(trs, k)) for k in FEATURE_COLUMNS},
            thesis_direction=trs.thesis_direction.value,
            expected_horizon=trs.expected_horizon,
            pit_safe=trs.pit_safe, research_only=trs.research_only,
        )
        self.append(snap)
        return snap

    def load_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def trainable_rows(self, as_of: str = "") -> list[dict[str, Any]]:
        """The ONLY rows a training set may contain.

        Two independent exclusions, both hard:

          * `research_only` / `pit_safe=false` — a snapshot generated by
            pointing an LLM at a past date knows how the story ended. It is
            legitimate research output and it is never a training feature.
          * `available_at > as_of` — a snapshot that did not exist yet at the
            decision time cannot inform that decision.

        Readiness counting applied the first rule; nothing applied either at
        the point where rows actually become training data. This is that point.
        """
        rows = [r for r in self.load_all()
                if r.get("pit_safe") and not r.get("research_only")]
        if as_of:
            rows = [r for r in rows if str(r.get("available_at") or "") <= as_of]
        return rows

    # ── dataset readiness (第三十七/三十八/三十九条) ──

    def status(self, *, min_snapshots: int = 500, min_unique_dates: int = 60,
               min_unique_instruments: int = 15) -> dict[str, Any]:
        rows = [r for r in self.load_all() if r.get("pit_safe") and not r.get("research_only")]
        now = datetime.now(timezone.utc).isoformat()
        matured = [r for r in rows if r.get("mature_at", "9999") <= now]
        dates = {r["generated_at"][:10] for r in rows}
        instruments = {r["instrument_id"] for r in rows}
        if not rows:
            state = "COLLECTING"
        elif (len(matured) >= min_snapshots and len(dates) >= min_unique_dates
              and len(instruments) >= min_unique_instruments):
            state = "READY_FOR_EXPERIMENT"
        else:
            state = "COLLECTING" if len(rows) < min_snapshots else "INSUFFICIENT_SAMPLE"
        by_model: dict[str, int] = {}
        for r in rows:
            key = f"{r.get('model', '?')}@{(r.get('prompt_versions') or {}).get('version', '?')}"
            by_model[key] = by_model.get(key, 0) + 1
        return {
            "state": state,
            "pit_safe_snapshots": len(rows),
            "matured_snapshots": len(matured),
            "unique_dates": len(dates),
            "unique_instruments": len(instruments),
            "model_distribution": by_model,
            "thresholds": {"min_snapshots": min_snapshots,
                           "min_unique_dates": min_unique_dates,
                           "min_unique_instruments": min_unique_instruments},
            "training_allowed": state == "READY_FOR_EXPERIMENT",
        }

    def feature_distribution(self) -> dict[str, dict[str, float]]:
        """Per-feature mean/std grouped by model+prompt version → drift watch."""
        rows = self.load_all()
        groups: dict[str, list[dict]] = {}
        for r in rows:
            key = f"{r.get('model', '?')}@{(r.get('prompt_versions') or {}).get('version', '?')}"
            groups.setdefault(key, []).append(r)
        out: dict[str, dict[str, float]] = {}
        for key, grp in groups.items():
            stats: dict[str, float] = {"n": len(grp)}
            for col in FEATURE_COLUMNS:
                vals = [float((g.get("features") or {}).get(col, 0.0)) for g in grp]
                if vals:
                    mean = sum(vals) / len(vals)
                    stats[f"{col}_mean"] = round(mean, 4)
                    stats[f"{col}_std"] = round((sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5, 4)
            out[key] = stats
        return out
