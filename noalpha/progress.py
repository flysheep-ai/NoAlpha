from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol


class InvestigationStage(str, Enum):
    QUEUED = "queued"
    RESOLVING_INSTRUMENT = "resolving_instrument"
    FETCHING_MARKET_DATA = "fetching_market_data"
    FETCHING_SEC_FACTS = "fetching_sec_facts"
    FETCHING_FILINGS = "fetching_filings"
    FETCHING_NEWS = "fetching_news"
    NORMALIZING_EVIDENCE = "normalizing_evidence"
    CANONICALIZING = "canonicalizing"
    DEDUPLICATING = "deduplicating"
    APPLYING_PIT_FILTER = "applying_pit_filter"
    SCORING_QUALITY = "scoring_quality"
    BUILDING_TIMELINE = "building_timeline"
    DETECTING_EVENTS = "detecting_events"
    BUILDING_RESEARCH_CONTEXT = "building_research_context"
    PLANNING_RESEARCH = "planning_research"
    ANALYZING_MARKET = "analyzing_market"
    ANALYZING_FUNDAMENTALS = "analyzing_fundamentals"
    ANALYZING_FILINGS = "analyzing_filings"
    ANALYZING_NEWS = "analyzing_news"
    CROSS_SOURCE_SYNTHESIS = "cross_source_synthesis"
    BUILDING_BULL_CASE = "building_bull_case"
    BUILDING_BEAR_CASE = "building_bear_case"
    RUNNING_CRITIC = "running_critic"
    GENERATING_CLAIMS = "generating_claims"
    VALIDATING_CITATIONS = "validating_citations"
    CALCULATING_CONFIDENCE = "calculating_confidence"
    FINAL_SYNTHESIS = "final_synthesis"
    COMPLETED = "completed"
    FAILED = "failed"


STAGE_ORDER: list[InvestigationStage] = [
    InvestigationStage.QUEUED,
    InvestigationStage.RESOLVING_INSTRUMENT,
    InvestigationStage.FETCHING_MARKET_DATA,
    InvestigationStage.FETCHING_SEC_FACTS,
    InvestigationStage.FETCHING_FILINGS,
    InvestigationStage.FETCHING_NEWS,
    InvestigationStage.NORMALIZING_EVIDENCE,
    InvestigationStage.CANONICALIZING,
    InvestigationStage.DEDUPLICATING,
    InvestigationStage.APPLYING_PIT_FILTER,
    InvestigationStage.SCORING_QUALITY,
    InvestigationStage.BUILDING_TIMELINE,
    InvestigationStage.DETECTING_EVENTS,
    InvestigationStage.BUILDING_RESEARCH_CONTEXT,
    InvestigationStage.PLANNING_RESEARCH,
    InvestigationStage.ANALYZING_MARKET,
    InvestigationStage.ANALYZING_FUNDAMENTALS,
    InvestigationStage.ANALYZING_FILINGS,
    InvestigationStage.ANALYZING_NEWS,
    InvestigationStage.CROSS_SOURCE_SYNTHESIS,
    InvestigationStage.BUILDING_BULL_CASE,
    InvestigationStage.BUILDING_BEAR_CASE,
    InvestigationStage.RUNNING_CRITIC,
    InvestigationStage.GENERATING_CLAIMS,
    InvestigationStage.VALIDATING_CITATIONS,
    InvestigationStage.CALCULATING_CONFIDENCE,
    InvestigationStage.FINAL_SYNTHESIS,
    InvestigationStage.COMPLETED,
]


@dataclass
class StageProgress:
    stage: str
    status: str  # pending / running / completed / skipped / degraded / failed
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: float = 0.0
    message: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "message": self.message,
            "metrics": self.metrics,
            "error": self.error,
        }


@dataclass
class InvestigationProgress:
    investigation_id: str
    status: str = "queued"
    current_stage: str = "queued"
    stage_index: int = 0
    total_stages: int = len(STAGE_ORDER)
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    stages: dict[str, StageProgress] = field(default_factory=dict)
    provider_status: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigation_id": self.investigation_id,
            "status": self.status,
            "current_stage": self.current_stage,
            "stage_index": self.stage_index,
            "total_stages": self.total_stages,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "provider_status": self.provider_status,
            "warnings": self.warnings,
            "degraded": self.degraded,
        }


class ProgressReporter(Protocol):
    def stage_started(self, stage: InvestigationStage, message: str = "") -> None: ...
    def stage_completed(self, stage: InvestigationStage, message: str = "", metrics: dict[str, Any] | None = None) -> None: ...
    def stage_degraded(self, stage: InvestigationStage, message: str = "", metrics: dict[str, Any] | None = None) -> None: ...
    def stage_failed(self, stage: InvestigationStage, error: str, message: str = "") -> None: ...
    def emit_metric(self, key: str, value: Any) -> None: ...
    def set_provider_status(self, provider: str, status: str) -> None: ...
    def add_warning(self, warning: str) -> None: ...


class PersistentProgressReporter:
    def __init__(self, investigation_id: str) -> None:
        self.progress = InvestigationProgress(investigation_id=investigation_id)
        self.progress.started_at = datetime.now(timezone.utc).isoformat()
        self.progress.updated_at = self.progress.started_at
        self._init_stages()

    def _init_stages(self) -> None:
        for stage in STAGE_ORDER:
            self.progress.stages[stage.value] = StageProgress(
                stage=stage.value, status="pending"
            )

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _update_progress(self, stage: InvestigationStage, status: str) -> None:
        self.progress.current_stage = stage.value
        idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else self.progress.stage_index
        self.progress.stage_index = idx
        self.progress.updated_at = self._now()

    def stage_started(self, stage: InvestigationStage, message: str = "") -> None:
        sp = self.progress.stages[stage.value]
        sp.status = "running"
        sp.started_at = self._now()
        sp.message = message
        self._update_progress(stage, "running")

    def stage_completed(self, stage: InvestigationStage, message: str = "", metrics: dict[str, Any] | None = None) -> None:
        sp = self.progress.stages[stage.value]
        sp.status = "completed"
        sp.completed_at = self._now()
        if sp.started_at:
            from datetime import datetime as dt
            try:
                start = dt.fromisoformat(sp.started_at)
                end = dt.fromisoformat(sp.completed_at)
                sp.duration_ms = (end - start).total_seconds() * 1000
            except Exception:
                pass
        if message:
            sp.message = message
        if metrics:
            sp.metrics.update(metrics)
        self._update_progress(stage, "running")

    def stage_degraded(self, stage: InvestigationStage, message: str = "", metrics: dict[str, Any] | None = None) -> None:
        sp = self.progress.stages[stage.value]
        sp.status = "degraded"
        sp.completed_at = self._now()
        sp.message = message
        if metrics:
            sp.metrics.update(metrics)
        self.progress.degraded = True
        self._update_progress(stage, "running")

    def stage_failed(self, stage: InvestigationStage, error: str, message: str = "") -> None:
        sp = self.progress.stages[stage.value]
        sp.status = "failed"
        sp.completed_at = self._now()
        sp.error = error
        sp.message = message or error
        self._update_progress(stage, "running")

    def emit_metric(self, key: str, value: Any) -> None:
        self._update_progress(self.progress.current_stage, "running")

    def set_provider_status(self, provider: str, status: str) -> None:
        self.progress.provider_status[provider] = status

    def add_warning(self, warning: str) -> None:
        self.progress.warnings.append(warning)
