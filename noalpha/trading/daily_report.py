"""Daily Trading Report domain & persistence.

Generated automatically at end-of-day by the ContinuousIntelligenceLoop.
Structured, not free-form LLM text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DecisionQuality(str, Enum):
    GOOD_DECISION_GOOD_OUTCOME = "good_good"
    GOOD_DECISION_BAD_OUTCOME = "good_bad"
    BAD_DECISION_GOOD_OUTCOME = "bad_good"
    BAD_DECISION_BAD_OUTCOME = "bad_bad"
    UNKNOWN = "unknown"


class MarketRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    ROTATION = "rotation"
    UNKNOWN = "unknown"


REGIME_LABELS_ZH: dict[MarketRegime, str] = {
    MarketRegime.TRENDING_UP: "趋势上行", MarketRegime.TRENDING_DOWN: "趋势下行",
    MarketRegime.RANGE_BOUND: "区间震荡", MarketRegime.HIGH_VOLATILITY: "高波动",
    MarketRegime.RISK_ON: "风险偏好", MarketRegime.RISK_OFF: "风险回避",
    MarketRegime.ROTATION: "风格轮动", MarketRegime.UNKNOWN: "未知",
}

REGIME_LABELS_EN: dict[MarketRegime, str] = {
    MarketRegime.TRENDING_UP: "Trending Up", MarketRegime.TRENDING_DOWN: "Trending Down",
    MarketRegime.RANGE_BOUND: "Range Bound", MarketRegime.HIGH_VOLATILITY: "High Volatility",
    MarketRegime.RISK_ON: "Risk On", MarketRegime.RISK_OFF: "Risk Off",
    MarketRegime.ROTATION: "Rotation", MarketRegime.UNKNOWN: "Unknown",
}


# ── structured components ──

@dataclass
class TradeReviewItem:
    proposal_id: str
    symbol: str
    name: str
    action: str
    decision_time: str
    thesis: str
    evidence_ids: list[str]
    target_weight: float
    final_weight: float
    price: float
    quantity: int
    pnl_contribution: float
    decision_quality: DecisionQuality
    quality_reason: str
    lessons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id, "symbol": self.symbol, "name": self.name,
            "action": self.action, "decision_time": self.decision_time,
            "thesis": self.thesis, "evidence_ids": self.evidence_ids,
            "target_weight": self.target_weight, "final_weight": self.final_weight,
            "price": self.price, "quantity": self.quantity,
            "pnl_contribution": round(self.pnl_contribution, 2),
            "decision_quality": self.decision_quality.value,
            "quality_reason": self.quality_reason, "lessons": self.lessons,
        }


@dataclass
class MissedOpportunity:
    symbol: str
    name: str
    reason_missed: str
    candidate_score: float
    subsequent_return: float
    would_have_pnl: float
    lesson: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "name": self.name, "reason_missed": self.reason_missed,
            "candidate_score": self.candidate_score,
            "subsequent_return": round(self.subsequent_return, 4),
            "would_have_pnl": round(self.would_have_pnl, 2),
            "lesson": self.lesson,
        }


@dataclass
class DailyLesson:
    lesson_id: str
    title: str
    description: str
    evidence_decision_ids: list[str]
    evidence_market_data: str
    confidence: float
    applicable_scope: str  # e.g. "momentum_strategy", "CN_market", "high_vol_regime"
    recurrence_status: str  # ONE_OFF / POSSIBLE_PATTERN / RECURRING_PATTERN
    candidate_improvement: str  # improvement_id or empty
    status: str = "proposed"  # proposed / replay_evaluating / shadow / approved / rejected

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id, "title": self.title,
            "description": self.description,
            "evidence_decision_ids": self.evidence_decision_ids,
            "evidence_market_data": self.evidence_market_data,
            "confidence": self.confidence, "applicable_scope": self.applicable_scope,
            "recurrence_status": self.recurrence_status,
            "candidate_improvement": self.candidate_improvement,
            "status": self.status,
        }


@dataclass
class CandidateImprovement:
    improvement_id: str
    source_lesson_ids: list[str]
    hypothesis: str
    expected_benefit: str
    risks: list[str]
    status: str  # PROPOSED / OFFLINE_EVAL / SHADOW / PAPER / APPROVED / REJECTED
    proposed_prompt_changes: dict[str, Any]
    replay_results: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "improvement_id": self.improvement_id, "source_lesson_ids": self.source_lesson_ids,
            "hypothesis": self.hypothesis, "expected_benefit": self.expected_benefit,
            "risks": self.risks, "status": self.status,
            "proposed_prompt_changes": self.proposed_prompt_changes,
            "replay_results": self.replay_results,
        }


# ── main report ──

@dataclass
class DailyTradingReport:
    report_id: str
    session_id: str
    date: str  # YYYY-MM-DD
    market: str  # home market
    generated_at: str

    # 1. market summary
    market_regime: MarketRegime
    market_regime_reason: str
    index_performance: dict[str, float]
    sector_performance: dict[str, float]

    # 2. portfolio performance
    opening_equity: float
    closing_equity: float
    daily_pnl: float
    daily_return: float
    benchmark_return: float
    excess_return: float
    max_intraday_drawdown: float
    turnover: float
    total_commissions: float

    # 3. trades
    trade_count: int
    buy_count: int
    sell_count: int
    successful_reviews: list[TradeReviewItem]
    failed_reviews: list[TradeReviewItem]

    # 4. missed opportunities
    no_action_decisions: int
    risk_rejections: int
    missed_opportunities: list[MissedOpportunity]

    # 5. execution quality
    avg_slippage: float
    partial_fills: int
    missed_fills: int
    avg_fill_latency_s: float

    # 6. thesis changes
    thesis_changes: list[dict[str, Any]]

    # 7. lessons
    lessons: list[DailyLesson]
    candidate_improvements: list[CandidateImprovement]

    # 8. tomorrow plan
    tomorrow_plan: str
    tomorrow_watchlist: list[str]
    tomorrow_pending_actions: list[dict[str, Any]]
    tomorrow_unknowns: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "session_id": self.session_id,
            "date": self.date, "market": self.market, "generated_at": self.generated_at,
            "market_regime": self.market_regime.value,
            "market_regime_reason": self.market_regime_reason,
            "index_performance": self.index_performance,
            "sector_performance": self.sector_performance,
            "opening_equity": self.opening_equity,
            "closing_equity": self.closing_equity,
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_return": round(self.daily_return, 4),
            "benchmark_return": round(self.benchmark_return, 4),
            "excess_return": round(self.excess_return, 4),
            "max_intraday_drawdown": round(self.max_intraday_drawdown, 4),
            "turnover": round(self.turnover, 2),
            "total_commissions": round(self.total_commissions, 2),
            "trade_count": self.trade_count, "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "successful_reviews": [r.to_dict() for r in self.successful_reviews],
            "failed_reviews": [r.to_dict() for r in self.failed_reviews],
            "no_action_decisions": self.no_action_decisions,
            "risk_rejections": self.risk_rejections,
            "missed_opportunities": [m.to_dict() for m in self.missed_opportunities],
            "avg_slippage": round(self.avg_slippage, 6),
            "partial_fills": self.partial_fills,
            "missed_fills": self.missed_fills,
            "avg_fill_latency_s": round(self.avg_fill_latency_s, 2),
            "thesis_changes": self.thesis_changes,
            "lessons": [l.to_dict() for l in self.lessons],
            "candidate_improvements": [c.to_dict() for c in self.candidate_improvements],
            "tomorrow_plan": self.tomorrow_plan,
            "tomorrow_watchlist": self.tomorrow_watchlist,
            "tomorrow_pending_actions": self.tomorrow_pending_actions,
            "tomorrow_unknowns": self.tomorrow_unknowns,
        }


def determine_market_regime(index_changes: dict[str, float]) -> MarketRegime:
    """Simple deterministic regime classifier from index performance."""
    avg = sum(index_changes.values()) / max(len(index_changes), 1)
    extremes = [abs(v) for v in index_changes.values()]
    max_extreme = max(extremes) if extremes else 0

    if max_extreme > 0.03:
        return MarketRegime.HIGH_VOLATILITY
    if avg > 0.01:
        return MarketRegime.TRENDING_UP if max_extreme < 0.025 else MarketRegime.RISK_ON
    if avg < -0.01:
        return MarketRegime.TRENDING_DOWN if max_extreme < 0.025 else MarketRegime.RISK_OFF
    return MarketRegime.RANGE_BOUND
