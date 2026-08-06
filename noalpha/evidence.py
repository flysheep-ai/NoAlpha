from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from noalpha.domain import (
    Evidence,
    EvidenceType,
    FinancialFact,
    Filing,
    MarketBar,
    NewsItem,
    stable_id,
)


def market_bar_to_evidence(bar: MarketBar) -> Evidence:
    evidence_id = stable_id("market_bar", bar.instrument.symbol, bar.timestamp.isoformat(), bar.source)
    content = (
        f"{bar.instrument.symbol} daily bar: open {bar.open}, high {bar.high}, "
        f"low {bar.low}, close {bar.close}, volume {bar.volume}."
    )
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.MARKET_BAR,
        source=bar.source,
        source_url=None,
        event_time=bar.timestamp,
        published_at=None,
        available_at=bar.available_at,
        retrieved_at=bar.observed_at,
        content=content,
        metadata={
            "symbol": bar.instrument.symbol,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": bar.volume,
        },
    )


def filing_to_evidence(filing: Filing) -> Evidence:
    evidence_id = stable_id("filing", filing.instrument.symbol, filing.accession_number, filing.filing_type)
    period = filing.period_end.date().isoformat() if filing.period_end else "unknown period"
    content = f"{filing.instrument.symbol} filed {filing.filing_type} for {period}."
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.FILING,
        source=filing.source,
        source_url=filing.source_url,
        event_time=filing.filing_date,
        published_at=filing.accepted_at,
        available_at=filing.accepted_at,
        retrieved_at=filing.retrieved_at,
        content=content,
        metadata={
            "symbol": filing.instrument.symbol,
            "filing_type": filing.filing_type,
            "filing_date": filing.filing_date.isoformat(),
            "accepted_at": filing.accepted_at.isoformat(),
            "period_end": filing.period_end.isoformat() if filing.period_end else None,
            "accession_number": filing.accession_number,
            "content_hash": filing.content_hash,
        },
    )


def financial_fact_to_evidence(fact: FinancialFact) -> Evidence:
    period_type = "duration" if fact.period_start else "instant"
    duration_days = (fact.period_end - fact.period_start).days if fact.period_start else None
    source_observation_id = stable_id(
        "source_observation",
        fact.instrument.symbol,
        fact.taxonomy,
        fact.concept,
        fact.unit,
        fact.period_start.isoformat() if fact.period_start else None,
        fact.period_end.isoformat(),
        fact.value,
        fact.accession_number,
        fact.form,
    )
    evidence_id = stable_id(
        "financial_fact",
        fact.instrument.symbol,
        fact.concept,
        fact.unit,
        fact.period_end.isoformat(),
        fact.filed_at.isoformat(),
        fact.value,
    )
    content = (
        f"{fact.instrument.symbol} {fact.concept} was {fact.value} {fact.unit} "
        f"for period ending {fact.period_end.date().isoformat()}."
    )
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.FINANCIAL_FACT,
        source=fact.source,
        source_url=fact.source_url,
        event_time=fact.period_end,
        published_at=fact.accepted_at or fact.filed_at,
        available_at=fact.accepted_at or fact.filed_at,
        retrieved_at=fact.retrieved_at,
        content=content,
        metadata={
            "symbol": fact.instrument.symbol,
            "concept": fact.concept,
            "value": str(fact.value),
            "unit": fact.unit,
            "period_start": fact.period_start.isoformat() if fact.period_start else None,
            "period_end": fact.period_end.isoformat(),
            "filed_at": fact.filed_at.isoformat(),
            "accepted_at": fact.accepted_at.isoformat() if fact.accepted_at else None,
            "form": fact.form,
            "taxonomy": fact.taxonomy,
            "fiscal_year": fact.fiscal_year,
            "fiscal_period": fact.fiscal_period,
            "accession_number": fact.accession_number,
            "frame": fact.frame,
            "dimensions": fact.dimensions,
            "period_type": period_type,
            "duration_days": duration_days,
            "source_observation_id": source_observation_id,
            "availability_uncertain": fact.accepted_at is None,
        },
    )


def news_item_to_evidence(item: NewsItem) -> Evidence:
    evidence_id = stable_id("news", item.source, item.source_url, item.published_at.isoformat(), item.title)
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.NEWS,
        source=item.source,
        source_url=item.source_url,
        event_time=item.published_at,
        published_at=item.published_at,
        available_at=item.published_at,
        retrieved_at=item.retrieved_at,
        content=f"{item.title}\n{item.content}".strip(),
        metadata={
            "title": item.title,
            "language": item.language,
            "symbols": list(item.symbols),
        },
    )


def normalize_all(
    bars: Iterable[MarketBar] = (),
    filings: Iterable[Filing] = (),
    facts: Iterable[FinancialFact] = (),
    news: Iterable[NewsItem] = (),
) -> list[Evidence]:
    evidence = [market_bar_to_evidence(item) for item in bars]
    evidence.extend(filing_to_evidence(item) for item in filings)
    evidence.extend(financial_fact_to_evidence(item) for item in facts)
    evidence.extend(news_item_to_evidence(item) for item in news)
    return dedupe_evidence(evidence)


def dedupe_evidence(items: Iterable[Evidence]) -> list[Evidence]:
    by_id: dict[str, Evidence] = {}
    for item in items:
        if item.evidence_id not in by_id:
            by_id[item.evidence_id] = item
    return list(by_id.values())
