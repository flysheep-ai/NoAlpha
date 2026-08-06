"""Portfolio construction baselines: Equal Weight & Inverse Volatility
(第六十/六十三条), with max-weight constraint and turnover penalty.

More elaborate optimizers (min-variance, HRP, CVaR) stay out until these
baselines have out-of-sample evidence against them — complexity is not a
default win (第六十二条; see adoption matrix).
"""
from __future__ import annotations

from typing import Any

from noalpha.quant.domain import PortfolioTarget


class PortfolioOptimizer:
    """Interface: expected returns + risk estimates + current book → targets."""
    method = "base"

    def __init__(self, max_weight: float = 0.15, total_exposure: float = 0.9,
                 turnover_penalty: float = 0.25) -> None:
        self.max_weight = max_weight
        self.total_exposure = total_exposure
        # fraction of the proposed *change* that is discarded — a cheap,
        # explicit way to avoid full-book churn every rebalance
        self.turnover_penalty = turnover_penalty

    def _raw_weights(self, candidates: dict[str, dict[str, Any]]) -> dict[str, float]:
        raise NotImplementedError

    def construct(self, candidates: dict[str, dict[str, Any]],
                  current_weights: dict[str, float] | None = None) -> PortfolioTarget:
        """candidates: symbol -> {expected_return, volatility, ...}."""
        current = dict(current_weights or {})
        raw = self._raw_weights(candidates)
        # cap + renormalize to total_exposure
        capped = {s: min(w, self.max_weight) for s, w in raw.items()}
        total = sum(capped.values())
        if total > 0:
            scale = min(self.total_exposure / total, 1.0)
            capped = {s: w * scale for s, w in capped.items()}
        # turnover penalty: move only (1 - penalty) of the way from current
        target = {}
        for s in set(capped) | set(current):
            want = capped.get(s, 0.0)
            have = current.get(s, 0.0)
            target[s] = have + (want - have) * (1.0 - self.turnover_penalty)
        target = {s: round(w, 4) for s, w in target.items() if w > 0.001}
        turnover = sum(abs(target.get(s, 0.0) - current.get(s, 0.0))
                       for s in set(target) | set(current))
        return PortfolioTarget(
            method=self.method, weights=target, turnover=round(turnover, 4),
            constraints={"max_weight": self.max_weight, "total_exposure": self.total_exposure,
                         "turnover_penalty": self.turnover_penalty},
            inputs={s: {k: v for k, v in c.items() if isinstance(v, (int, float, str))}
                    for s, c in candidates.items()},
        )


class EqualWeightOptimizer(PortfolioOptimizer):
    method = "equal_weight"

    def _raw_weights(self, candidates: dict[str, dict[str, Any]]) -> dict[str, float]:
        if not candidates:
            return {}
        w = 1.0 / len(candidates)
        return {s: w for s in candidates}


class InverseVolatilityOptimizer(PortfolioOptimizer):
    """Weights ∝ 1/volatility (skfolio naive InverseVolatility protocol)."""
    method = "inverse_volatility"

    def _raw_weights(self, candidates: dict[str, dict[str, Any]]) -> dict[str, float]:
        inv = {}
        for s, c in candidates.items():
            vol = c.get("volatility") or 0.0
            inv[s] = 1.0 / vol if vol and vol > 0 else 0.0
        if all(v == 0 for v in inv.values()) and candidates:
            return {s: 1.0 / len(candidates) for s in candidates}
        total = sum(inv.values()) or 1.0
        return {s: v / total for s, v in inv.items()}


OPTIMIZERS = {"equal_weight": EqualWeightOptimizer, "inverse_volatility": InverseVolatilityOptimizer}
