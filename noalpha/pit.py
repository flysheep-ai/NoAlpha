from __future__ import annotations

from noalpha.domain import AsOfContext, Evidence
from noalpha.errors import PointInTimeViolation


def filter_evidence_as_of(evidence: list[Evidence], context: AsOfContext) -> list[Evidence]:
    # available_at governs availability; additionally an event that occurs after
    # as_of cannot be evidence at as_of, even if its available_at claims otherwise
    # (contradictory data — fail closed). retrieved_at is deliberately NOT guarded:
    # replay/backtests legitimately fetch historical data after as_of.
    return [
        item for item in evidence
        if context.allows(item.available_at)
        and not (item.event_time and item.event_time > context.as_of)
    ]


def assert_available_as_of(evidence: Evidence, context: AsOfContext) -> None:
    if not context.allows(evidence.available_at):
        raise PointInTimeViolation(
            f"Evidence {evidence.evidence_id} available at {evidence.available_at.isoformat()} "
            f"cannot be used as of {context.as_of.isoformat()}"
        )
