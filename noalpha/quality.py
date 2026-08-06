from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from noalpha.domain import Claim, ClaimType, Evidence, EvidenceType, stable_id


METRIC_MAP: dict[str, tuple[str, float, str]] = {
    "Revenues": ("revenue", 0.9, "income_statement"),
    "RevenueFromContractWithCustomerExcludingAssessedTax": ("revenue", 0.9, "income_statement"),
    "SalesRevenueNet": ("revenue", 0.85, "income_statement"),
    "NetIncomeLoss": ("net_income", 0.9, "income_statement"),
    "OperatingIncomeLoss": ("operating_income", 0.85, "income_statement"),
    "EarningsPerShareDiluted": ("diluted_eps", 0.8, "per_share"),
    "WeightedAverageNumberOfDilutedSharesOutstanding": ("diluted_share_count", 0.7, "capitalization"),
    "Assets": ("assets", 0.75, "balance_sheet"),
    "Liabilities": ("liabilities", 0.75, "balance_sheet"),
    "StockholdersEquity": ("stockholders_equity", 0.75, "balance_sheet"),
    "CashAndCashEquivalentsAtCarryingValue": ("cash", 0.75, "balance_sheet"),
}


def canonicalize_evidence(evidence: list[Evidence], as_of: datetime) -> tuple[list[Evidence], dict[str, Any]]:
    raw_count = len(evidence)
    future = [item for item in evidence if item.available_at > as_of]
    exact_deduped, exact_duplicates = _dedupe_exact(evidence)
    financial, other = _split_financial(exact_deduped)
    canonical_financial, financial_report = _canonicalize_financial(financial, as_of)
    canonical_other, other_duplicates = _dedupe_non_financial(other)
    canonical_pool = canonical_other + canonical_financial
    scored = [_with_importance(item, as_of) for item in canonical_pool]
    relevant, low_value_count, stale_count = _apply_relevance_gate(scored, as_of)
    relevant.sort(key=lambda item: (item.event_time, item.evidence_id))

    duplicates_by_type = Counter()
    duplicates_by_type["exact"] = exact_duplicates
    duplicates_by_type["source"] = other_duplicates
    duplicates_by_type["financial"] = financial_report["financial_duplicate_count"]
    duplicate_count = sum(duplicates_by_type.values())
    availability_uncertain = sum(1 for item in relevant if item.metadata.get("availability_uncertain"))
    report = {
        "raw_evidence_count": raw_count,
        "canonical_evidence_count": len(relevant),
        "canonical_pool_count": len(canonical_pool),
        "duplicate_count": duplicate_count,
        "duplicates_by_type": dict(duplicates_by_type),
        "financial_raw_count": len(financial),
        "financial_canonical_count": len(canonical_financial),
        "financial_duplicate_count": financial_report["financial_duplicate_count"],
        "low_value_facts": low_value_count,
        "stale_financial_facts": stale_count,
        "high_value_evidence": sum(1 for item in relevant if float(item.metadata.get("importance_score", 0.0)) >= 0.7),
        "availability_uncertain": availability_uncertain,
        "future_evidence_rejected": len(future),
        "amended_observations": financial_report["amended_observations"],
        "conflicting_financial_observations": financial_report["conflicting_observations"],
    }
    return relevant, report


def economic_observation_id(item: Evidence) -> str:
    metadata = item.metadata
    dimensions = metadata.get("dimensions") or {}
    normalized_dimensions = tuple(sorted((str(k), str(v)) for k, v in dimensions.items()))
    metric = _canonical_metric(str(metadata.get("concept")))[0]
    return stable_id(
        "economic_observation",
        metadata.get("symbol"),
        metadata.get("taxonomy") or "us-gaap",
        metric,
        metadata.get("unit"),
        metadata.get("period_start"),
        metadata.get("period_end"),
        metadata.get("period_type"),
        normalized_dimensions,
    )


def comparable_period_key(item: Evidence) -> tuple[Any, ...]:
    metadata = item.metadata
    duration = metadata.get("duration_days")
    if metadata.get("period_type") == "instant":
        bucket = "instant"
    elif duration is None:
        bucket = "duration_unknown"
    elif 70 <= int(duration) <= 110:
        bucket = "quarter"
    elif 160 <= int(duration) <= 210:
        bucket = "half_year_ytd"
    elif 250 <= int(duration) <= 300:
        bucket = "nine_month_ytd"
    elif 330 <= int(duration) <= 390:
        bucket = "annual"
    else:
        bucket = f"duration_{duration}"
    return (
        metadata.get("canonical_metric") or _canonical_metric(str(metadata.get("concept")))[0],
        metadata.get("unit"),
        bucket,
        metadata.get("fiscal_period") if bucket != "instant" else None,
        tuple(sorted((metadata.get("dimensions") or {}).items())),
    )


def evaluate_claim_citations(claims: Iterable[Claim], evidence: Iterable[Evidence]) -> dict[str, Any]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    counts = Counter()
    details = []
    for claim in claims:
        missing = [ref for ref in claim.evidence_ids if ref not in evidence_by_id]
        if missing:
            status = "unsupported"
        elif claim.claim_type == ClaimType.HYPOTHESIS:
            status = "partially_supported" if claim.evidence_ids else "unsupported"
        elif claim.evidence_ids:
            status = "supported"
        else:
            status = "unsupported"
        counts[status] += 1
        details.append({"claim_id": claim.claim_id, "status": status, "missing_evidence_ids": missing})
    total = sum(counts.values())
    support_rate = (counts["supported"] + 0.5 * counts["partially_supported"]) / total if total else 0.0
    return {
        "claim_count": total,
        "supported_claims": counts["supported"],
        "partially_supported_claims": counts["partially_supported"],
        "unsupported_claims": counts["unsupported"],
        "contradictory_claims": counts["contradictory"],
        "citation_support_rate": round(support_rate, 2),
        "details": details,
    }


def _dedupe_exact(evidence: Iterable[Evidence]) -> tuple[list[Evidence], int]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[Evidence] = []
    duplicates = 0
    for item in evidence:
        if item.evidence_type == EvidenceType.FINANCIAL_FACT:
            key = (
                item.evidence_type,
                item.metadata.get("source_observation_id"),
                item.metadata.get("period_start"),
                item.metadata.get("period_end"),
                item.metadata.get("period_type"),
                item.metadata.get("value"),
            )
        else:
            key = (item.evidence_type, item.source, item.source_url, item.event_time, item.available_at, item.content)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        deduped.append(item)
    return deduped, duplicates


def _split_financial(evidence: Iterable[Evidence]) -> tuple[list[Evidence], list[Evidence]]:
    financial = []
    other = []
    for item in evidence:
        if item.evidence_type == EvidenceType.FINANCIAL_FACT:
            financial.append(item)
        else:
            other.append(item)
    return financial, other


def _dedupe_non_financial(evidence: Iterable[Evidence]) -> tuple[list[Evidence], int]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[Evidence] = []
    duplicates = 0
    for item in evidence:
        source_key = item.source_url or item.content.lower()
        key = (item.evidence_type, item.source, source_key)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        deduped.append(item)
    return deduped, duplicates


def _canonicalize_financial(evidence: list[Evidence], as_of: datetime) -> tuple[list[Evidence], dict[str, int]]:
    by_observation: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        by_observation[economic_observation_id(item)].append(item)

    canonical: list[Evidence] = []
    duplicates = 0
    amended = 0
    conflicting = 0
    for observation_id, group in by_observation.items():
        usable_group = [item for item in group if item.available_at <= as_of]
        if not usable_group:
            continue
        duplicates += max(0, len(usable_group) - 1)
        forms = {str(item.metadata.get("form") or "") for item in usable_group}
        if any(form.endswith("/A") for form in forms):
            amended += 1
        values = {str(item.metadata.get("value")) for item in usable_group}
        if len(values) > 1:
            conflicting += 1
        winner = max(usable_group, key=lambda item: (item.available_at, _form_rank(str(item.metadata.get("form") or ""))))
        source_ids = tuple(sorted(str(item.metadata.get("source_observation_id") or item.evidence_id) for item in usable_group))
        metadata = dict(winner.metadata)
        metric, base_importance, category = _canonical_metric(str(metadata.get("concept")))
        metadata.update(
            {
                "economic_observation_id": observation_id,
                "source_observation_ids": list(source_ids),
                "source_observation_count": len(source_ids),
                "financial_duplicate_count": max(0, len(usable_group) - 1),
                "canonical_metric": metric,
                "metric_category": category,
                "metric_base_importance": base_importance,
                "amendment_status": "amended" if any(str(item.metadata.get("form") or "").endswith("/A") for item in usable_group) else "original_or_repeated",
                "has_conflicting_disclosures": len(values) > 1,
            }
        )
        canonical.append(replace(winner, evidence_id=stable_id("financial_canonical", observation_id), metadata=metadata))
    return canonical, {
        "financial_duplicate_count": duplicates,
        "amended_observations": amended,
        "conflicting_observations": conflicting,
    }


def _canonical_metric(concept: str) -> tuple[str, float, str]:
    return METRIC_MAP.get(concept, (concept, 0.25, "unknown"))


def _form_rank(form: str) -> int:
    if form.endswith("/A"):
        return 3
    if form in {"10-K", "10-Q"}:
        return 2
    if form == "8-K":
        return 1
    return 0


def _with_importance(item: Evidence, as_of: datetime) -> Evidence:
    metadata = dict(item.metadata)
    score = 0.2
    reasons: list[str] = []
    age_days = max(0, (as_of - item.event_time).days)
    recency = max(0.0, 1.0 - min(age_days, 3650) / 3650)
    if item.evidence_type == EvidenceType.FINANCIAL_FACT:
        base = float(metadata.get("metric_base_importance") or _canonical_metric(str(metadata.get("concept")))[1])
        duplicate_penalty = min(0.25, 0.04 * int(metadata.get("financial_duplicate_count") or 0))
        score = 0.55 * base + 0.25 * recency + 0.2 * _magnitude_score(metadata) - duplicate_penalty
        reasons.append(f"metric_base={base:.2f}")
        if duplicate_penalty:
            reasons.append(f"duplicate_penalty={duplicate_penalty:.2f}")
    elif item.evidence_type == EvidenceType.MARKET_BAR:
        score = 0.55 + 0.35 * recency
    elif item.evidence_type == EvidenceType.FILING:
        form = str(metadata.get("filing_type") or "")
        score = 0.85 if form in {"10-K", "10-Q", "8-K"} else 0.55
    elif item.evidence_type == EvidenceType.NEWS:
        score = 0.45 + 0.35 * recency
    metadata["importance_score"] = round(max(0.0, min(1.0, score)), 3)
    metadata["importance_reasons"] = reasons
    return replace(item, metadata=metadata)


def _magnitude_score(metadata: dict[str, Any]) -> float:
    try:
        value = abs(Decimal(str(metadata.get("value"))))
    except (InvalidOperation, TypeError):
        return 0.1
    if value >= Decimal("10000000000"):
        return 0.8
    if value >= Decimal("1000000000"):
        return 0.6
    if value >= Decimal("1000000"):
        return 0.35
    return 0.15


def _apply_relevance_gate(evidence: list[Evidence], as_of: datetime) -> tuple[list[Evidence], int, int]:
    cutoff = as_of - timedelta(days=365 * 6)
    latest_by_metric: dict[str, Evidence] = {}
    for item in evidence:
        if item.evidence_type != EvidenceType.FINANCIAL_FACT:
            continue
        metric = str(item.metadata.get("canonical_metric") or item.metadata.get("concept"))
        previous = latest_by_metric.get(metric)
        if previous is None or item.event_time > previous.event_time:
            latest_by_metric[metric] = item

    relevant: list[Evidence] = []
    low_value = 0
    stale = 0
    latest_ids = {item.evidence_id for item in latest_by_metric.values()}
    for item in evidence:
        if item.evidence_type != EvidenceType.FINANCIAL_FACT:
            relevant.append(item)
            continue
        importance = float(item.metadata.get("importance_score") or 0.0)
        if item.event_time < cutoff and item.evidence_id not in latest_ids:
            stale += 1
            continue
        if importance < 0.32 and item.evidence_id not in latest_ids:
            low_value += 1
            continue
        relevant.append(item)
    return relevant, low_value, stale
