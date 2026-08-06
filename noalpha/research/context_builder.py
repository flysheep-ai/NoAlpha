from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import uuid4

from noalpha.domain import DetectedEvent, Evidence, EvidenceType
from noalpha.research.context_fields import ResearchContext, SelectedEvidence


DEFAULT_PER_TYPE_QUOTA: dict[str, int] = {
    "market_bar": 8,
    "financial_fact": 12,
    "filing": 6,
    "news": 8,
}

CHARS_PER_TOKEN_ESTIMATE = 4


class ResearchContextBuilder:
    def __init__(
        self,
        max_tokens: int = 12000,
        per_type_quota: dict[str, int] | None = None,
    ):
        self.max_tokens = max_tokens
        self.per_type_quota = per_type_quota or DEFAULT_PER_TYPE_QUOTA

    def build(
        self,
        symbol: str,
        company_name: str,
        evidence: list[Evidence],
        events: tuple[DetectedEvent, ...],
        timeline_summary: str,
        quality_summary: dict[str, int],
        unknowns: list[str],
        as_of: datetime,
    ) -> ResearchContext:
        scored = self._score_evidence(evidence, events, as_of)
        selected, excluded_count = self._select_per_type(scored)

        token_estimate = sum(len(item.content) for item, _ in selected) // CHARS_PER_TOKEN_ESTIMATE
        if token_estimate > self.max_tokens:
            selected = selected[:self.max_tokens * CHARS_PER_TOKEN_ESTIMATE // max(1, sum(len(s[0].content) for s in selected) // len(selected))]
            token_estimate = sum(len(item.content) for item, _ in selected) // CHARS_PER_TOKEN_ESTIMATE

        modality_counts: dict[str, int] = {}
        for item, _ in selected:
            modality_counts[item.evidence_type.value] = modality_counts.get(item.evidence_type.value, 0) + 1

        present_types = {item.evidence_type for item, _ in selected}
        all_types = {EvidenceType.MARKET_BAR, EvidenceType.FINANCIAL_FACT, EvidenceType.FILING, EvidenceType.NEWS}
        missing_modalities = [t.value for t in all_types if t not in present_types]

        event_summaries = [f"{e.event_type.value}: {e.description}" for e in events]

        context_id = hashlib.sha256(
            f"{symbol}-{as_of.isoformat()}-{uuid4()}".encode()
        ).hexdigest()[:16]

        return ResearchContext(
            context_id=context_id,
            as_of=as_of,
            symbol=symbol,
            company_name=company_name,
            selected_evidence=selected,
            excluded_count=excluded_count,
            token_estimate=token_estimate,
            modality_counts=modality_counts,
            missing_modalities=missing_modalities,
            event_summaries=event_summaries,
            timeline_summary=timeline_summary,
            quality_summary=quality_summary,
            unknowns_summary=unknowns,
        )

    def _score_evidence(
        self,
        evidence: list[Evidence],
        events: tuple[DetectedEvent, ...],
        as_of: datetime,
    ) -> list[tuple[Evidence, float, str]]:
        event_ref_ids: set[str] = set()
        for event in events:
            event_ref_ids.update(event.evidence_refs)

        results: list[tuple[Evidence, float, str]] = []
        now = as_of

        for item in evidence:
            score = 0.0
            reasons: list[str] = []

            # 1. Event relevance (highest priority)
            if item.evidence_id in event_ref_ids:
                score += 100.0
                reasons.append("event_reference")

            # 2. Importance (from metadata)
            importance = float(item.metadata.get("importance", 0.5))
            score += importance * 50.0

            # 3. Recency
            days_old = (now - item.event_time).days if item.event_time else 365
            recency = max(0.0, 1.0 - days_old / 365.0)
            score += recency * 30.0

            # 4. Source quality bonus
            source = item.source.lower()
            if "sec" in source or "edgar" in source:
                score += 20.0
            elif "marketdata" in source:
                score += 10.0

            reasons.append(f"importance={importance:.2f}")
            reasons.append(f"recency={recency:.2f}")

            results.append((item, score, "; ".join(reasons)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def _select_per_type(
        self, scored: list[tuple[Evidence, float, str]]
    ) -> tuple[list[tuple[Evidence, str]], int]:
        selected: list[tuple[Evidence, str]] = []
        counts: dict[str, int] = {}

        for item, score, reason in scored:
            etype = item.evidence_type.value
            quota = self.per_type_quota.get(etype, 6)
            if counts.get(etype, 0) < quota:
                selected.append((item, reason))
                counts[etype] = counts.get(etype, 0) + 1

        excluded = len(scored) - len(selected)
        return selected, excluded
