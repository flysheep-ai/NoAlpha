from __future__ import annotations

from noalpha.domain import Evidence, EvidenceType, TimelineEvent


def build_timeline(evidence: list[Evidence]) -> tuple[TimelineEvent, ...]:
    events = [to_timeline_event(item) for item in evidence]
    events.sort(key=lambda item: (item.timestamp, item.evidence_id))
    return tuple(events)


def to_timeline_event(evidence: Evidence) -> TimelineEvent:
    if evidence.evidence_type == EvidenceType.MARKET_BAR:
        title = f"Market bar close {evidence.metadata.get('close')} volume {evidence.metadata.get('volume')}"
    elif evidence.evidence_type == EvidenceType.FILING:
        title = f"Filing {evidence.metadata.get('filing_type')}"
    elif evidence.evidence_type == EvidenceType.FINANCIAL_FACT:
        title = f"{evidence.metadata.get('concept')} = {evidence.metadata.get('value')} {evidence.metadata.get('unit')}"
    elif evidence.evidence_type == EvidenceType.NEWS:
        title = str(evidence.metadata.get("title") or evidence.content.splitlines()[0])
    else:
        title = evidence.content.splitlines()[0]
    return TimelineEvent(
        timestamp=evidence.event_time,
        evidence_id=evidence.evidence_id,
        title=title,
        source=evidence.source,
        source_url=evidence.source_url,
        metadata={"evidence_type": evidence.evidence_type.value, **evidence.metadata},
    )
