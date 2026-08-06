"""Forward shadow comparison of the three experiment arms (Phase Q5).

Arm A quant_only            — quant signal alone decides
Arm B deterministic_hybrid  — quant + deterministic research ensemble (the
                              pre-Q5 baseline, deliberately preserved)
Arm C llm_hybrid            — quant + REAL LLM research fusion

Only one arm drives the real VirtualBroker (session.research_mode decides);
the others run as weight-space shadow portfolios over the SAME quotes, the
SAME cost assumptions and the SAME cycle timing, so the comparison is fair
(第四十六条). Shadow books hold target weights, mark them with real quote
moves each cycle, and charge round-trip costs on weight changes.

Fairness limits are stated, not hidden: shadow books ignore lot sizes and
intraday fills — they measure signal quality, not microstructure.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ARMS = ("quant_only", "deterministic_hybrid", "llm_hybrid")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DecisionDelta:
    """Same instrument, same cycle — what each arm would do (第四十九条)."""
    delta_id: str = field(default_factory=lambda: f"dd-{uuid.uuid4().hex[:10]}")
    as_of: str = field(default_factory=_now)
    cycle: int = 0
    symbol: str = ""
    arm_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    llm_changed_decision: bool = False
    change_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ShadowPortfolio:
    """Weight-space paper book: weights + cash share, marked by quote moves."""

    def __init__(self, arm: str, initial_capital: float,
                 round_trip_cost: float = 0.003) -> None:
        self.arm = arm
        self.initial_capital = initial_capital
        self.equity = initial_capital
        self.weights: dict[str, float] = {}
        self.round_trip_cost = round_trip_cost
        self.trade_count = 0
        self.fees_paid = 0.0

    def mark(self, day_returns: dict[str, float]) -> None:
        """Apply per-cycle instrument returns to held weights."""
        growth = 1.0
        for sym, w in self.weights.items():
            growth += w * day_returns.get(sym, 0.0)
        self.equity *= growth
        # re-normalize drifted weights so the next rebalance sees post-move book
        if growth > 0:
            self.weights = {s: w * (1 + day_returns.get(s, 0.0)) / growth
                            for s, w in self.weights.items()}

    def rebalance(self, targets: dict[str, float]) -> None:
        turnover = 0.0
        for sym in set(targets) | set(self.weights):
            turnover += abs(targets.get(sym, 0.0) - self.weights.get(sym, 0.0))
        if turnover > 1e-6:
            cost = turnover / 2.0 * self.round_trip_cost   # one side per leg
            self.fees_paid += cost * self.equity
            self.equity *= (1 - cost)
            self.trade_count += sum(
                1 for s in set(targets) | set(self.weights)
                if abs(targets.get(s, 0.0) - self.weights.get(s, 0.0)) > 1e-4)
        self.weights = {s: w for s, w in targets.items() if w > 1e-4}

    def to_dict(self) -> dict[str, Any]:
        return {"arm": self.arm, "equity": round(self.equity, 2),
                "return_pct": round((self.equity / self.initial_capital - 1) * 100, 3),
                "weights": {k: round(v, 4) for k, v in self.weights.items()},
                "trade_count": self.trade_count,
                "fees_paid": round(self.fees_paid, 2)}


class ShadowArena:
    """Holds the three arm books + append-only delta/equity logs."""

    def __init__(self, session_id: str, initial_capital: float,
                 root: str | Path = ".cache/quant/shadow",
                 round_trip_cost: float = 0.003) -> None:
        self.session_id = session_id
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.books = {arm: ShadowPortfolio(arm, initial_capital, round_trip_cost)
                      for arm in ARMS}
        self.deltas_path = self.root / f"{session_id}_deltas.jsonl"
        self.equity_path = self.root / f"{session_id}_equity.jsonl"
        self.llm_stats = {"calls": 0, "tokens": 0, "latency_ms_total": 0.0,
                          "failures": 0, "fallback_cycles": 0, "vetoes": 0,
                          "confirmations": 0, "reduces_triggered": 0}

    def record_delta(self, delta: DecisionDelta) -> None:
        with self.deltas_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(delta.to_dict(), ensure_ascii=False) + "\n")

    def step(self, cycle: int, day_returns: dict[str, float],
             arm_targets: dict[str, dict[str, float]]) -> dict[str, Any]:
        """Mark all books with this cycle's returns, then apply each arm's
        target weights. Returns the equity row (also appended to disk)."""
        for arm, book in self.books.items():
            book.mark(day_returns)
            if arm in arm_targets:
                book.rebalance(arm_targets[arm])
        row = {"as_of": _now(), "cycle": cycle,
               **{arm: self.books[arm].to_dict() for arm in ARMS},
               "llm_stats": dict(self.llm_stats)}
        with self.equity_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def load_deltas(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.deltas_path.exists():
            return []
        lines = self.deltas_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]

    def summary(self) -> dict[str, Any]:
        deltas = self.load_deltas(limit=10000)
        changed = [d for d in deltas if d.get("llm_changed_decision")]
        return {
            "session_id": self.session_id,
            "arms": {arm: self.books[arm].to_dict() for arm in ARMS},
            "decision_deltas_total": len(deltas),
            "llm_changed_decisions": len(changed),
            "llm_stats": dict(self.llm_stats),
            "sample_sufficiency": "INSUFFICIENT_SAMPLE",  # honest until weeks of data
        }
