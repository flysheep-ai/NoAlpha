from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from noalpha.domain import Evidence, EvidenceType


SOURCE_QUALITY = {
    "SEC EDGAR companyfacts": 0.95,
    "SEC EDGAR submissions": 0.95,
    "MarketData.app prototype candles": 0.5,
    "Yahoo Finance prototype chart": 0.45,
    "Yahoo Finance RSS": 0.55,
}


def calculate_confidence(evidence: list[Evidence], unknowns: list[str], as_of: datetime) -> tuple[float, list[str]]:
    overall, reasons, _ = calculate_confidence_breakdown(evidence, unknowns, as_of)
    return overall, reasons


def calculate_confidence_breakdown(
    evidence: list[Evidence],
    unknowns: list[str],
    as_of: datetime,
    *,
    quality_report: dict[str, Any] | None = None,
    citation_support_rate: float | None = None,
) -> tuple[float, list[str], dict[str, float]]:
    if not evidence:
        breakdown = {
            "market_data": 0.0,
            "fundamentals": 0.0,
            "filings": 0.0,
            "news": 0.0,
            "temporal_integrity": 0.0,
            "citation_support": 0.0,
            "duplication": 0.0,
        }
        return 0.0, ["No evidence available."], breakdown

    types = {item.evidence_type for item in evidence}
    by_type = {kind: [item for item in evidence if item.evidence_type == kind] for kind in EvidenceType}
    market = _component_score(by_type[EvidenceType.MARKET_BAR], as_of, cap=0.9)
    fundamentals = _component_score(by_type[EvidenceType.FINANCIAL_FACT], as_of, cap=0.95)
    filings = _component_score(by_type[EvidenceType.FILING], as_of, cap=0.98)
    news = _component_score(by_type[EvidenceType.NEWS], as_of, cap=0.72)
    temporal = 1.0
    quality = quality_report or {}
    if quality.get("future_evidence_rejected"):
        temporal -= min(0.4, 0.05 * int(quality["future_evidence_rejected"]))
    if quality.get("availability_uncertain"):
        temporal -= min(0.25, 0.005 * int(quality["availability_uncertain"]))
    temporal = max(0.0, temporal)
    raw = int(quality.get("raw_evidence_count") or len(evidence))
    duplicates = int(quality.get("duplicate_count") or 0)
    duplicate_ratio = duplicates / raw if raw else 0.0
    duplication = max(0.2, 1.0 - duplicate_ratio)
    citation = citation_support_rate if citation_support_rate is not None else 0.7
    if EvidenceType.MARKET_BAR not in types:
        market = min(market, 0.15)
    if EvidenceType.NEWS not in types:
        news = min(news, 0.2)
    if EvidenceType.FINANCIAL_FACT not in types:
        fundamentals = min(fundamentals, 0.15)
    if EvidenceType.FILING not in types:
        filings = min(filings, 0.25)

    breakdown = {
        "market_data": round(market, 2),
        "fundamentals": round(fundamentals, 2),
        "filings": round(filings, 2),
        "news": round(news, 2),
        "temporal_integrity": round(temporal, 2),
        "citation_support": round(citation, 2),
        "duplication": round(duplication, 2),
    }
    weights = {
        "market_data": 0.17,
        "fundamentals": 0.22,
        "filings": 0.15,
        "news": 0.12,
        "temporal_integrity": 0.16,
        "citation_support": 0.13,
        "duplication": 0.05,
    }
    score = sum(breakdown[name] * weight for name, weight in weights.items())
    if unknowns:
        score -= min(0.18, 0.03 * len(unknowns))
    latest = max(item.available_at for item in evidence)
    if as_of - latest > timedelta(days=120):
        score -= 0.15
    if any("prototype" in item.source.lower() or item.source == "Yahoo Finance RSS" for item in evidence):
        score = min(score, 0.92)
    score = max(0.0, min(1.0, score))
    reasons = [
        f"{len(evidence)} evidence items from {len({item.source for item in evidence})} sources.",
        f"Evidence types: {', '.join(sorted(item.value for item in types))}.",
        f"Duplicate ratio after quality gate input: {duplicate_ratio:.2%}.",
    ]
    if unknowns:
        reasons.append(f"{len(unknowns)} unknowns reduce confidence.")
    return round(score, 2), reasons, breakdown


def _component_score(items: list[Evidence], as_of: datetime, *, cap: float) -> float:
    if not items:
        return 0.0
    qualities = [SOURCE_QUALITY.get(item.source, 0.4) for item in items]
    quality = sum(qualities) / len(qualities)
    latest = max(item.available_at for item in items)
    age_days = max(0, (as_of - latest).days)
    freshness = max(0.0, 1.0 - min(age_days, 365) / 365)
    count_score = min(1.0, len(items) / 20)
    return min(cap, 0.55 * quality + 0.3 * freshness + 0.15 * count_score)
