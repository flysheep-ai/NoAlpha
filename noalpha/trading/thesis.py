from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from noalpha.trading.proposal import ExitReason


class ThesisStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    STRENGTHENED = "strengthened"
    WEAKENED = "weakened"
    CHALLENGED = "challenged"
    INVALIDATED = "invalidated"
    CLOSED = "closed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ThesisEvent:
    event_id: str = field(default_factory=lambda: str(uuid4()))
    thesis_id: str = ""
    event_type: str = ""  # created / strengthened / weakened / challenged / invalidated / closed
    description: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    triggered_by: str = ""  # new_evidence / market_event / risk_check / manual
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "thesis_id": self.thesis_id,
            "event_type": self.event_type, "description": self.description,
            "evidence_ids": self.evidence_ids, "triggered_by": self.triggered_by,
            "timestamp": self.timestamp,
        }


@dataclass
class InvestmentThesis:
    thesis_id: str = field(default_factory=lambda: str(uuid4()))
    symbol: str = ""
    status: ThesisStatus = ThesisStatus.PROPOSED
    thesis_text: str = ""
    catalyst: str = ""
    supporting_evidence_ids: list[str] = field(default_factory=list)
    counter_evidence_ids: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    expected_horizon: str = "20-60 trading days"
    confidence: float = 0.0
    proposal_id: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    closed_at: str = ""
    events: list[ThesisEvent] = field(default_factory=list)
    exit_reason: ExitReason | None = None
    exit_rationale: str = ""
    realized_pnl: float = 0.0

    def record_event(self, event_type: str, description: str, evidence_ids: list[str] | None = None,
                     triggered_by: str = "manual") -> ThesisEvent:
        evt = ThesisEvent(
            thesis_id=self.thesis_id, event_type=event_type,
            description=description,
            evidence_ids=evidence_ids or [],
            triggered_by=triggered_by,
        )
        self.events.append(evt)
        self.updated_at = _now()
        return evt

    def strengthen(self, description: str, evidence_ids: list[str] | None = None) -> None:
        self.status = ThesisStatus.STRENGTHENED
        self.record_event("strengthened", description, evidence_ids, "new_evidence")

    def weaken(self, description: str, evidence_ids: list[str] | None = None) -> None:
        self.status = ThesisStatus.WEAKENED
        self.record_event("weakened", description, evidence_ids, "new_evidence")

    def challenge(self, description: str) -> None:
        self.status = ThesisStatus.CHALLENGED
        self.record_event("challenged", description, triggered_by="risk_check")

    def invalidate(self, reason: str, evidence_ids: list[str] | None = None) -> None:
        self.status = ThesisStatus.INVALIDATED
        self.record_event("invalidated", reason, evidence_ids, "new_evidence")

    def close(self, exit_reason: ExitReason, rationale: str, realized_pnl: float = 0.0) -> None:
        self.status = ThesisStatus.CLOSED
        self.exit_reason = exit_reason
        self.exit_rationale = rationale
        self.realized_pnl = realized_pnl
        self.closed_at = _now()
        self.record_event("closed", f"{exit_reason.value}: {rationale}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_id": self.thesis_id, "symbol": self.symbol,
            "status": self.status.value, "thesis_text": self.thesis_text,
            "catalyst": self.catalyst,
            "supporting_evidence_ids": self.supporting_evidence_ids,
            "counter_evidence_ids": self.counter_evidence_ids,
            "risks": self.risks,
            "invalidation_conditions": self.invalidation_conditions,
            "expected_horizon": self.expected_horizon,
            "confidence": self.confidence,
            "proposal_id": self.proposal_id,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "events": [e.to_dict() for e in self.events],
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "exit_rationale": self.exit_rationale,
            "realized_pnl": self.realized_pnl,
        }
