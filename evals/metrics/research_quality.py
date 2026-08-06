from __future__ import annotations

from datetime import datetime
from typing import Any


def evaluate_replay_case(result: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    as_of = _parse_dt(case["as_of"])
    evidence = result["evidence"]
    future_evidence = [item["evidence_id"] for item in evidence if _parse_dt(item["available_at"]) > as_of]
    event_types = {event["event_type"] for event in result["report"]["detected_events"]}
    missing_expected_events = [event for event in case.get("expected_major_events", []) if event not in event_types]
    unknowns_text = " | ".join(result["report"].get("unknowns", []))
    missing_expected_unknowns = [
        item for item in case.get("expected_unknowns", []) if item.lower() not in unknowns_text.lower()
    ]
    citation = result["quality_report"].get("citation_support", {})
    return {
        "case_id": case["case_id"],
        "symbol": case["symbol"],
        "status": "pass" if not future_evidence and not missing_expected_unknowns and not missing_expected_events else "review",
        "future_evidence_count": len(future_evidence),
        "future_evidence_ids": future_evidence[:20],
        "missing_expected_events": missing_expected_events,
        "missing_expected_unknowns": missing_expected_unknowns,
        "citation_support_rate": citation.get("citation_support_rate"),
        "overall_confidence": result["report"]["confidence"],
        "confidence_breakdown": result["report"]["confidence_breakdown"],
        "quality_summary": {
            "raw_evidence_count": result["quality_report"].get("raw_evidence_count"),
            "canonical_evidence_count": result["quality_report"].get("canonical_evidence_count"),
            "duplicate_count": result["quality_report"].get("duplicate_count"),
            "availability_uncertain": result["quality_report"].get("availability_uncertain"),
        },
    }


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
