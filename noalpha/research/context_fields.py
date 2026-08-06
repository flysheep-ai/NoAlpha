from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from noalpha.domain import Evidence, EvidenceType
from noalpha.llm.schemas import LLMUsage


@dataclass(frozen=True)
class SelectedEvidence:
    evidence_id: str
    evidence_type: str
    selection_reason: str
    importance: float
    recency_score: float


@dataclass(frozen=True)
class ResearchContext:
    context_id: str
    as_of: datetime
    symbol: str
    company_name: str
    selected_evidence: list[tuple[Evidence, str]]  # (evidence, reason)
    excluded_count: int
    token_estimate: int
    modality_counts: dict[str, int]
    missing_modalities: list[str]
    event_summaries: list[str]
    timeline_summary: str
    quality_summary: dict[str, int]
    unknowns_summary: list[str]


@dataclass(frozen=True)
class ResearchQuestion:
    id: str
    question: str
    priority: str  # high / medium / low
    required_evidence_types: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResearchPlan:
    questions: list[ResearchQuestion]
    required_evidence_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AnalysisResult:
    title: str
    summary: str
    key_findings: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(frozen=True)
class BullCaseResult:
    thesis: str
    supporting_claims: list[str] = field(default_factory=list)
    catalysts: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BearCaseResult:
    thesis: str
    supporting_claims: list[str] = field(default_factory=list)
    downside_drivers: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CritiqueFinding:
    type: str
    description: str
    severity: str  # high / medium / low
    affected_claim_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CritiqueResult:
    findings: list[CritiqueFinding] = field(default_factory=list)
    overall_assessment: str = ""


@dataclass(frozen=True)
class ResearchUnknown:
    description: str
    category: str  # missing_data / unconfirmed_source / no_filing / no_guidance / other
    importance: str  # high / medium / low


@dataclass(frozen=True)
class ValidatedClaim:
    claim_id: str
    text: str
    claim_type: str  # observation / inference / hypothesis
    evidence_ids: list[str] = field(default_factory=list)
    inference_level: str = ""
    confidence: float = 0.0
    caveats: str = ""
    citation_status: str = ""  # supported / partially_supported / unsupported / contradicted


@dataclass(frozen=True)
class ResearchRunMetadata:
    research_mode: str
    llm_provider: str = ""
    model: str = ""
    prompt_versions: dict[str, str] = field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    latency_ms: float = 0.0
    token_usage: LLMUsage = field(default_factory=LLMUsage)
    retry_count: int = 0
    context_id: str = ""
    selected_evidence_count: int = 0
    excluded_evidence_count: int = 0
