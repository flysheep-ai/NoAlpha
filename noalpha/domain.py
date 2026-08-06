from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import uuid4


UTC = timezone.utc


class Market(str, Enum):
    US = "US"
    A_SHARE = "A_SHARE"
    HK = "HK"


class EvidenceType(str, Enum):
    MARKET_BAR = "market_bar"
    FILING = "filing"
    FINANCIAL_FACT = "financial_fact"
    NEWS = "news"
    EVENT = "event"


class ClaimType(str, Enum):
    OBSERVATION = "observation"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"


class EventType(str, Enum):
    ABNORMAL_RETURN = "abnormal_return"
    ABNORMAL_VOLUME = "abnormal_volume"
    NEW_FILING = "new_filing"
    MAJOR_FINANCIAL_CHANGE = "major_financial_change"
    NEWS_BURST = "news_burst"


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) == 10:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def stable_id(*parts: object) -> str:
    digest = sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class AsOfContext:
    as_of: datetime

    @classmethod
    def now(cls) -> "AsOfContext":
        return cls(now_utc())

    def allows(self, available_at: datetime | None) -> bool:
        if available_at is None:
            return False
        if available_at.tzinfo is None:
            available_at = available_at.replace(tzinfo=UTC)
        return available_at <= self.as_of


@dataclass(frozen=True)
class Instrument:
    symbol: str
    exchange: str
    market: Market
    currency: str
    company_name: str
    sector: str | None = None
    industry: str | None = None
    cik: str | None = None


@dataclass(frozen=True)
class MarketBar:
    instrument: Instrument
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class Filing:
    instrument: Instrument
    filing_type: str
    filing_date: datetime
    accepted_at: datetime
    period_end: datetime | None
    source_url: str
    source: str
    retrieved_at: datetime
    content_hash: str
    accession_number: str | None = None


@dataclass(frozen=True)
class FinancialFact:
    instrument: Instrument
    concept: str
    value: Decimal
    unit: str
    period_start: datetime | None
    period_end: datetime
    filed_at: datetime
    accepted_at: datetime | None
    source: str
    source_url: str
    retrieved_at: datetime
    form: str | None = None
    taxonomy: str = "us-gaap"
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    accession_number: str | None = None
    frame: str | None = None
    dimensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NewsItem:
    title: str
    published_at: datetime
    retrieved_at: datetime
    source: str
    source_url: str
    content: str
    language: str
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    evidence_type: EvidenceType
    source: str
    source_url: str | None
    event_time: datetime
    published_at: datetime | None
    available_at: datetime
    retrieved_at: datetime
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TimelineEvent:
    timestamp: datetime
    evidence_id: str
    title: str
    source: str
    source_url: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DetectedEvent:
    event_id: str
    event_type: EventType
    instrument: Instrument
    event_time: datetime
    description: str
    severity: float
    evidence_refs: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str
    claim_type: ClaimType
    evidence_ids: tuple[str, ...]
    inference_level: str
    confidence: float


@dataclass(frozen=True)
class ResearchReport:
    subject: Instrument
    generated_at: datetime
    as_of: datetime
    summary: str
    bull_case: list[str]
    bear_case: list[str]
    risks: list[str]
    unknowns: list[str]
    evidence_refs: tuple[str, ...]
    confidence: float
    confidence_reasons: list[str]
    timeline: tuple[TimelineEvent, ...]
    detected_events: tuple[DetectedEvent, ...]
    confidence_breakdown: dict[str, float] = field(default_factory=dict)
    claims: tuple[Claim, ...] = ()
    quality_report: dict[str, Any] = field(default_factory=dict)
    investigation_id: str = field(default_factory=lambda: str(uuid4()))

    def to_dict(self) -> dict[str, Any]:
        def dt(value: datetime | None) -> str | None:
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None

        return {
            "investigation_id": self.investigation_id,
            "subject": {
                "symbol": self.subject.symbol,
                "exchange": self.subject.exchange,
                "market": self.subject.market.value,
                "currency": self.subject.currency,
                "company_name": self.subject.company_name,
                "sector": self.subject.sector,
                "industry": self.subject.industry,
                "cik": self.subject.cik,
            },
            "generated_at": dt(self.generated_at),
            "as_of": dt(self.as_of),
            "summary": self.summary,
            "bull_case": self.bull_case,
            "bear_case": self.bear_case,
            "risks": self.risks,
            "unknowns": self.unknowns,
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
            "confidence_reasons": self.confidence_reasons,
            "confidence_breakdown": self.confidence_breakdown,
            "quality_report": self.quality_report,
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "text": claim.text,
                    "claim_type": claim.claim_type.value,
                    "evidence_ids": list(claim.evidence_ids),
                    "inference_level": claim.inference_level,
                    "confidence": claim.confidence,
                }
                for claim in self.claims
            ],
            "timeline": [
                {
                    "timestamp": dt(item.timestamp),
                    "evidence_id": item.evidence_id,
                    "title": item.title,
                    "source": item.source,
                    "source_url": item.source_url,
                    "metadata": item.metadata,
                }
                for item in self.timeline
            ],
            "detected_events": [
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type.value,
                    "event_time": dt(event.event_time),
                    "description": event.description,
                    "severity": event.severity,
                    "evidence_refs": list(event.evidence_refs),
                    "metadata": event.metadata,
                }
                for event in self.detected_events
            ],
        }
