"""SignalEnsemble: deterministic first-version combination of quant, LLM
research and event signals (第五十一/五十二条), with explicit conflict
handling instead of silent averaging (第五十四条).

Weights are configuration, not learned — a learned meta-model needs its own
out-of-sample evidence first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from noalpha.quant.domain import (
    ConflictResolution, QuantSignal, SignalConflict, SignalDirection,
)


@dataclass
class EnsembleInput:
    symbol: str
    quant: QuantSignal | None = None
    llm_direction: str = ""            # bullish | bearish | neutral | "" (absent)
    llm_confidence: float = 0.0
    llm_mode: str = ""                 # llm | deterministic
    event_risk: float = 0.0            # 0..1 severity of negative events
    cross_market_risk: float = 0.0     # 0..1


@dataclass
class EnsembleDecision:
    symbol: str
    action_hint: str                   # buy | hold | avoid | reduce
    combined_score: float              # 0..1
    components: dict[str, Any] = field(default_factory=dict)
    conflict: SignalConflict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "action_hint": self.action_hint,
                "combined_score": round(self.combined_score, 4),
                "components": self.components,
                "conflict": self.conflict.to_dict() if self.conflict else None}


DEFAULT_WEIGHTS = {"quant": 0.45, "research": 0.35, "event": 0.12, "cross_market": 0.08}


class SignalEnsemble:
    def __init__(self, weights: dict[str, float] | None = None,
                 buy_threshold: float = 0.60, avoid_threshold: float = 0.40) -> None:
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        self.buy_threshold = buy_threshold
        self.avoid_threshold = avoid_threshold

    @staticmethod
    def _quant_score(sig: QuantSignal | None) -> tuple[float, str]:
        if sig is None:
            return 0.5, "absent"
        base = sig.confidence if sig.calibrated else 0.5 + (sig.strength - 0.5) * 0.6
        if sig.direction == SignalDirection.SHORT:
            base = 1.0 - base if sig.calibrated else 0.5 - sig.strength * 0.3
        return max(0.0, min(1.0, base)), sig.direction.value

    @staticmethod
    def _research_score(direction: str, confidence: float) -> tuple[float, str]:
        if not direction:
            return 0.5, "absent"
        if direction == "bullish":
            return 0.5 + confidence * 0.5, direction
        if direction == "bearish":
            return 0.5 - confidence * 0.5, direction
        return 0.5, direction

    def combine(self, inp: EnsembleInput) -> EnsembleDecision:
        q_score, q_dir = self._quant_score(inp.quant)
        r_score, r_dir = self._research_score(inp.llm_direction, inp.llm_confidence)
        e_score = 1.0 - inp.event_risk           # 1 = no negative events
        x_score = 1.0 - inp.cross_market_risk
        w = self.weights
        combined = (q_score * w["quant"] + r_score * w["research"]
                    + e_score * w["event"] + x_score * w["cross_market"])
        combined = max(0.0, min(1.0, combined))

        components = {
            "quant": {"score": round(q_score, 3), "direction": q_dir,
                      "model": inp.quant.model_version if inp.quant else "",
                      "calibrated": bool(inp.quant.calibrated) if inp.quant else False,
                      "weight": w["quant"]},
            "research": {"score": round(r_score, 3), "direction": r_dir,
                         "mode": inp.llm_mode, "weight": w["research"]},
            "event": {"score": round(e_score, 3), "weight": w["event"]},
            "cross_market": {"score": round(x_score, 3), "weight": w["cross_market"]},
        }

        # ── conflict detection: strong quant vs opposing research view ──
        conflict = None
        quant_bullish = q_dir == "long" and q_score >= 0.6
        quant_bearish = q_dir == "short" or q_score <= 0.4
        research_present = bool(inp.llm_direction) and inp.llm_direction != "neutral"
        if research_present and ((quant_bullish and r_dir == "bearish")
                                 or (quant_bearish and r_dir == "bullish")):
            resolution = (ConflictResolution.REDUCE_POSITION
                          if abs(q_score - r_score) < 0.35
                          else ConflictResolution.NO_ACTION)
            conflict = SignalConflict(
                symbol=inp.symbol,
                quant_view=f"{q_dir} (score {q_score:.2f})",
                llm_view=f"{r_dir} (confidence {inp.llm_confidence:.2f})",
                detail="quant model and research disagree on direction; "
                       "not averaged away (第五十四条)",
                resolution=resolution,
            )
            action = "reduce" if resolution == ConflictResolution.REDUCE_POSITION else "avoid"
            return EnsembleDecision(symbol=inp.symbol, action_hint=action,
                                    combined_score=combined, components=components,
                                    conflict=conflict)

        if combined >= self.buy_threshold:
            action = "buy"
        elif combined <= self.avoid_threshold:
            action = "avoid"
        else:
            action = "hold"
        return EnsembleDecision(symbol=inp.symbol, action_hint=action,
                                combined_score=combined, components=components)
