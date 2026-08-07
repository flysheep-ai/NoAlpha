"""Who decides which instruments deserve deep research.

Before this module the answer was always the same heuristic momentum gate:

    if candidate.score < buy_score_threshold: continue

`candidate.score` is `opportunity_score(mom5, mom20, MA20, volume_ratio)` — a
hand-written scan heuristic. It ran even when an ACTIVE quant model was loaded
and ranking the whole universe, which meant the model's top-ranked names could
be dropped before Research ever saw them and the pipeline was, in truth:

    Heuristic Momentum Gate → Quant

This policy inverts that. When an ACTIVE quant model produced predictions, the
model's cross-sectional ranking is the gate and the heuristic score demotes to
what it actually is — a feature, an explanation and a tiebreak. The heuristic
gate survives only as an explicit fallback for when quant is unavailable, and
every selected candidate carries the provenance of which path chose it, so
System Truth and the UI can never show a quant-branded decision that a
momentum heuristic actually made.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

QUANT = "quant"
HEURISTIC_FALLBACK = "heuristic_fallback"


class _Candidate(Protocol):
    symbol: str
    score: float


@dataclass
class CandidateSelection:
    """The full, auditable result of one selection pass."""
    source: str = HEURISTIC_FALLBACK
    selected: list[Any] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def quant_driven(self) -> bool:
        return self.source == QUANT

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reason": self.reason,
            "selected": [{"symbol": getattr(c, "symbol", ""),
                          "heuristic_score": round(getattr(c, "score", 0.0), 4),
                          "candidate_source": getattr(c, "candidate_source", self.source),
                          "quant_rank": getattr(c, "quant_rank", None),
                          "quant_strength": getattr(c, "quant_strength", None)}
                         for c in self.selected],
            "rejected": self.rejected[:20],
            "rejected_total": len(self.rejected),
            "policy": self.policy,
        }


@dataclass
class CandidateSelectionPolicy:
    """Deterministic, versioned candidate gate.

    top_k                 how many ranked names may reach Research per cycle
    min_quant_strength    percentile-rank floor on the quant signal (0..1)
    heuristic_threshold   the OLD gate — used only when quant is unavailable
    require_long          only quant LONG-direction names are researched
    """
    version: str = "candidate-selection-policy:v1"
    top_k: int = 8
    min_quant_strength: float = 0.5
    heuristic_threshold: float = 0.60
    require_long: bool = True

    def select(self, candidates: list[Any], *,
               quant_available: bool,
               signals_by_symbol: dict[str, Any] | None = None,
               predictions_by_symbol: dict[str, Any] | None = None,
               quant_error: str = "") -> CandidateSelection:
        if quant_available:
            return self._select_by_quant(
                candidates, signals_by_symbol or {}, predictions_by_symbol or {})
        return self._select_by_heuristic(candidates, quant_error)

    # ── quant-led path ──

    def _select_by_quant(self, candidates: list[Any],
                         signals: dict[str, Any],
                         predictions: dict[str, Any]) -> CandidateSelection:
        out = CandidateSelection(
            source=QUANT,
            reason="ACTIVE quant model ranked the universe; the heuristic score is a "
                   "tiebreak only",
            policy={"version": self.version, "gate": "quant_rank",
                    "top_k": self.top_k, "min_quant_strength": self.min_quant_strength,
                    "require_long": self.require_long,
                    "heuristic_threshold_applied": False},
        )
        scored: list[tuple[float, Any]] = []
        for cand in candidates:
            sig = signals.get(cand.symbol)
            if sig is None:
                out.rejected.append({"symbol": cand.symbol, "reason": "no_quant_signal",
                                     "detail": "instrument produced no fresh features"})
                continue
            direction = getattr(sig.direction, "value", str(sig.direction))
            strength = float(getattr(sig, "strength", 0.0) or 0.0)
            if self.require_long and direction != "long":
                out.rejected.append({"symbol": cand.symbol, "reason": "quant_not_long",
                                     "quant_direction": direction, "quant_strength": strength})
                continue
            if strength < self.min_quant_strength:
                out.rejected.append({"symbol": cand.symbol, "reason": "quant_strength_below_floor",
                                     "quant_strength": strength,
                                     "floor": self.min_quant_strength})
                continue
            pred = predictions.get(cand.symbol)
            cand.candidate_source = QUANT
            cand.quant_strength = strength
            cand.quant_rank = getattr(pred, "predicted_rank", None)
            cand.selection_reason = (
                f"quant rank {cand.quant_rank}/{getattr(pred, 'universe_size', '?')}"
                if pred is not None else f"quant strength {strength:.2f}")
            # heuristic score is the TIEBREAK, never the gate
            scored.append((strength, cand))

        scored.sort(key=lambda pair: (pair[0], getattr(pair[1], "score", 0.0)), reverse=True)
        out.selected = [c for _, c in scored[:self.top_k]]
        for _, cand in scored[self.top_k:]:
            out.rejected.append({"symbol": cand.symbol, "reason": "below_top_k",
                                 "quant_strength": getattr(cand, "quant_strength", None),
                                 "top_k": self.top_k})
        return out

    # ── deterministic fallback ──

    def _select_by_heuristic(self, candidates: list[Any],
                             quant_error: str) -> CandidateSelection:
        out = CandidateSelection(
            source=HEURISTIC_FALLBACK,
            reason=f"no usable quant inference ({quant_error or 'unavailable'}); "
                   f"falling back to the momentum heuristic gate",
            policy={"version": self.version, "gate": "heuristic_score",
                    "top_k": self.top_k, "heuristic_threshold": self.heuristic_threshold,
                    "heuristic_threshold_applied": True,
                    "quant_error": quant_error},
        )
        passing: list[Any] = []
        for cand in candidates:
            score = float(getattr(cand, "score", 0.0) or 0.0)
            if score < self.heuristic_threshold:
                out.rejected.append({"symbol": cand.symbol,
                                     "reason": "heuristic_score_below_threshold",
                                     "score": round(score, 4),
                                     "threshold": self.heuristic_threshold})
                continue
            cand.candidate_source = HEURISTIC_FALLBACK
            cand.quant_strength = None
            cand.quant_rank = None
            cand.selection_reason = (f"heuristic score {score:.2f} ≥ "
                                     f"{self.heuristic_threshold:.2f} (quant unavailable)")
            passing.append(cand)
        passing.sort(key=lambda c: getattr(c, "score", 0.0), reverse=True)
        out.selected = passing[:self.top_k]
        for cand in passing[self.top_k:]:
            out.rejected.append({"symbol": cand.symbol, "reason": "below_top_k",
                                 "score": round(getattr(cand, "score", 0.0), 4),
                                 "top_k": self.top_k})
        return out
