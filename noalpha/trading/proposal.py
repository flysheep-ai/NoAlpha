from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    REDUCE = "reduce"
    INCREASE = "increase"
    HOLD = "hold"
    CLOSE = "close"
    NO_ACTION = "no_action"


class DecisionMode(str, Enum):
    """What ACTUALLY produced a decision — the system-truth provenance label.

    Never inferred by the frontend; computed each cycle from the real state of
    quant inference and the LLM research service."""
    DETERMINISTIC_SIGNAL = "deterministic_signal"      # momentum rules only
    QUANT_ONLY = "quant_only"                          # quant ranking, no research
    QUANT_DETERMINISTIC = "quant_deterministic"        # quant ranking + deterministic research
    LLM_RESEARCH = "llm_research"                      # LLM research, quant unavailable
    QUANT_LLM_HYBRID = "quant_llm_hybrid"              # both real
    QUANT_ONLY_FALLBACK = "quant_only_fallback"        # LLM requested but failed
    DETERMINISTIC_FALLBACK = "deterministic_fallback"  # quant AND/OR llm requested but unavailable

DECISION_MODE_LABEL_ZH: dict[str, str] = {
    "deterministic_signal": "确定性信号策略",
    "quant_only": "量化模型",
    "quant_deterministic": "量化排名 + 确定性研究",
    "llm_research": "LLM 深度研究",
    "quant_llm_hybrid": "量化 + LLM 混合",
    "quant_only_fallback": "量化（LLM 降级）",
    "deterministic_fallback": "确定性信号（降级）",
}


class ExitReason(str, Enum):
    THESIS_INVALIDATED = "thesis_invalidated"
    TARGET_REACHED = "target_reached"
    RISK_LIMIT = "risk_limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    REBALANCE = "rebalance"
    NEW_NEGATIVE_EVIDENCE = "new_negative_evidence"
    CONFIDENCE_DECAY = "confidence_decay"
    RANK_DETERIORATION = "rank_deterioration"
    TIME_EXIT = "time_exit"
    MANUAL = "manual"


@dataclass
class TradeRationale:
    thesis: str = ""
    catalyst: str = ""
    supporting_claim_ids: list[str] = field(default_factory=list)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    counter_arguments: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    expected_horizon: str = "20-60 trading days"
    confidence: float = 0.0
    # Explainability: every field below must be filled from real engine data,
    # never fabricated by the frontend.
    decision_summary: str = ""            # plain-language "why", from actual analysis factors
    why_this_instrument: str = ""         # candidate ranking vs. the rest of the universe
    why_now: list[str] = field(default_factory=list)      # concrete trigger signals
    signal_reason: str = ""               # raw signal summary (kept separate from thesis)
    catalysts: list[dict[str, Any]] = field(default_factory=list)  # {title, description, evidence_ids}
    target_weight_reason: dict[str, Any] = field(default_factory=dict)  # {base, confidence_multiplier, raw, cap, final}
    confidence_reason: dict[str, Any] = field(default_factory=dict)     # {positive: [...], negative: [...]}
    # Quant evidence: prediction/rank/model version + ensemble components,
    # filled from the real QuantInferenceService output (never fabricated).
    quant_view: dict[str, Any] = field(default_factory=dict)
    # LLM research evidence: structured TradingResearchSnapshot summary +
    # FusionDecision — only present when a REAL investigation ran.
    llm_view: dict[str, Any] = field(default_factory=dict)
    # TradingAgents-style role workflow: analyst team, bull/bear debate,
    # trader plan, risk guardians and fund-manager gate.
    agent_view: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis": self.thesis, "catalyst": self.catalyst,
            "supporting_claim_ids": self.supporting_claim_ids,
            "supporting_evidence_ids": self.supporting_evidence_ids,
            "counter_arguments": self.counter_arguments,
            "risks": self.risks,
            "invalidation_conditions": self.invalidation_conditions,
            "expected_horizon": self.expected_horizon,
            "confidence": self.confidence,
            "decision_summary": self.decision_summary,
            "why_this_instrument": self.why_this_instrument,
            "why_now": self.why_now,
            "signal_reason": self.signal_reason,
            "catalysts": self.catalysts,
            "target_weight_reason": self.target_weight_reason,
            "confidence_reason": self.confidence_reason,
            "quant_view": self.quant_view,
            "llm_view": self.llm_view,
            "agent_view": self.agent_view,
        }


@dataclass
class TradeProposal:
    proposal_id: str = field(default_factory=lambda: str(uuid4()))
    symbol: str = ""
    action: TradeAction = TradeAction.HOLD
    target_weight: float = 0.0
    rationale: TradeRationale = field(default_factory=TradeRationale)
    investigation_id: str = ""
    confidence: float = 0.0
    created_at: str = field(default_factory=_now)
    expires_at: str = ""
    status: str = "pending"  # pending / approved / adjusted / rejected / executed / expired
    risk_result: dict[str, Any] = field(default_factory=dict)
    # signal_only | evidence_grounded | hybrid — what the decision is actually based on
    decision_quality: str = "signal_only"
    # DecisionMode value — the true provenance of this decision (see DecisionMode)
    decision_mode: str = DecisionMode.DETERMINISTIC_SIGNAL.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id, "symbol": self.symbol,
            "action": self.action.value, "target_weight": self.target_weight,
            "rationale": self.rationale.to_dict(),
            "investigation_id": self.investigation_id,
            "confidence": self.confidence,
            "created_at": self.created_at, "expires_at": self.expires_at,
            "status": self.status, "risk_result": self.risk_result,
            "decision_quality": self.decision_quality,
            "decision_mode": self.decision_mode,
        }


@dataclass
class DecisionSnapshot:
    decision_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=_now)
    as_of: str = ""
    portfolio_equity: float = 0.0
    available_cash: float = 0.0
    positions: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] = field(default_factory=dict)
    risk_result: dict[str, Any] = field(default_factory=dict)
    final_action: str = ""
    model: str = ""
    prompt_versions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, "timestamp": self.timestamp,
            "as_of": self.as_of, "portfolio_equity": self.portfolio_equity,
            "available_cash": self.available_cash, "positions": self.positions,
            "proposal": self.proposal, "risk_result": self.risk_result,
            "final_action": self.final_action, "model": self.model,
        }
