from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from noalpha.domain import Instrument, now_utc


class TriggerType(str, Enum):
    ABNORMAL_RETURN = "abnormal_return"
    ABNORMAL_VOLUME = "abnormal_volume"
    NEW_FILING = "new_filing"
    NEWS_BURST = "news_burst"
    PROVIDER_HEALTH_CHANGE = "provider_health_change"


class CaseStatus(str, Enum):
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    OPEN = "OPEN"
    MONITORING = "MONITORING"
    UPDATED = "UPDATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class WatchDecision(str, Enum):
    CREATE_CASE = "create_case"
    UPDATE_CASE = "update_case"
    IGNORE = "ignore"
    COOLDOWN_SUPPRESSED = "cooldown_suppressed"


@dataclass(frozen=True)
class ScanPolicy:
    scan_interval_seconds: int = 3600
    lookback_days: int = 7
    return_threshold: float = 0.06
    volume_threshold: float = 2.5
    news_burst_threshold: int = 3
    filing_trigger_enabled: bool = True
    cooldown_seconds: int = 21600
    cluster_window_minutes: int = 30
    investigation_score_threshold: float = 0.65
    weights: dict[str, float] = field(
        default_factory=lambda: {
            TriggerType.ABNORMAL_RETURN.value: 0.45,
            TriggerType.ABNORMAL_VOLUME.value: 0.25,
            TriggerType.NEW_FILING.value: 0.65,
            TriggerType.NEWS_BURST.value: 0.35,
            TriggerType.PROVIDER_HEALTH_CHANGE.value: 0.2,
        }
    )

    @property
    def cooldown(self) -> timedelta:
        return timedelta(seconds=self.cooldown_seconds)

    @property
    def cluster_window(self) -> timedelta:
        return timedelta(minutes=self.cluster_window_minutes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_interval_seconds": self.scan_interval_seconds,
            "lookback_days": self.lookback_days,
            "return_threshold": self.return_threshold,
            "volume_threshold": self.volume_threshold,
            "news_burst_threshold": self.news_burst_threshold,
            "filing_trigger_enabled": self.filing_trigger_enabled,
            "cooldown_seconds": self.cooldown_seconds,
            "cluster_window_minutes": self.cluster_window_minutes,
            "investigation_score_threshold": self.investigation_score_threshold,
            "weights": self.weights,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScanPolicy":
        if not data:
            return cls()
        return cls(
            scan_interval_seconds=int(data.get("scan_interval_seconds", 3600)),
            lookback_days=int(data.get("lookback_days", 7)),
            return_threshold=float(data.get("return_threshold", 0.06)),
            volume_threshold=float(data.get("volume_threshold", 2.5)),
            news_burst_threshold=int(data.get("news_burst_threshold", 3)),
            filing_trigger_enabled=bool(data.get("filing_trigger_enabled", True)),
            cooldown_seconds=int(data.get("cooldown_seconds", 21600)),
            cluster_window_minutes=int(data.get("cluster_window_minutes", 30)),
            investigation_score_threshold=float(data.get("investigation_score_threshold", 0.65)),
            weights={str(k): float(v) for k, v in dict(data.get("weights") or cls().weights).items()},
        )


@dataclass(frozen=True)
class Watchlist:
    watchlist_id: str
    name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class WatchItem:
    watchlist_id: str
    instrument: Instrument
    enabled: bool
    scan_policy: ScanPolicy
    priority: int
    added_at: datetime
    last_scanned_at: datetime | None = None
    cooldown_until: datetime | None = None


@dataclass(frozen=True)
class TriggerCandidate:
    trigger_id: str
    instrument: Instrument
    trigger_type: TriggerType
    detected_at: datetime
    event_time: datetime
    severity: float
    score: float
    evidence_ids: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TriggerCluster:
    cluster_id: str
    instrument: Instrument
    window_start: datetime
    window_end: datetime
    trigger_types: tuple[TriggerType, ...]
    severity: float
    score: float
    trigger_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InvestigationCase:
    case_id: str
    instrument: Instrument
    status: CaseStatus
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    trigger_cluster_ids: tuple[str, ...]
    primary_trigger: str
    severity: float
    current_thesis: str
    counter_thesis: str
    unknowns: tuple[str, ...]
    confidence: float
    latest_report_id: str | None
    evidence_ids: tuple[str, ...]
    cooldown_until: datetime | None = None


@dataclass(frozen=True)
class ThesisSnapshot:
    snapshot_id: str
    case_id: str
    created_at: datetime
    summary: str
    bull_case: tuple[str, ...]
    bear_case: tuple[str, ...]
    risks: tuple[str, ...]
    unknowns: tuple[str, ...]
    confidence: float
    evidence_ids: tuple[str, ...]
    report: dict[str, Any]
    thesis_change: str


@dataclass(frozen=True)
class WatchScanSummary:
    scan_id: str
    watchlist_id: str
    scanned_at: datetime
    scan_count: int
    trigger_candidates: int
    trigger_clusters: int
    investigations_started: int
    cases_created: int
    cases_updated: int
    duplicate_cases_prevented: int
    cooldown_suppressed: int
    provider_failures: int
    degraded_symbols: tuple[str, ...]
    case_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "watchlist_id": self.watchlist_id,
            "scanned_at": self.scanned_at.isoformat().replace("+00:00", "Z"),
            "scan_count": self.scan_count,
            "trigger_candidates": self.trigger_candidates,
            "trigger_clusters": self.trigger_clusters,
            "investigations_started": self.investigations_started,
            "cases_created": self.cases_created,
            "cases_updated": self.cases_updated,
            "duplicate_cases_prevented": self.duplicate_cases_prevented,
            "cooldown_suppressed": self.cooldown_suppressed,
            "provider_failures": self.provider_failures,
            "degraded_symbols": list(self.degraded_symbols),
            "case_ids": list(self.case_ids),
        }


def default_watchlist(name: str = "default") -> Watchlist:
    ts = now_utc()
    return Watchlist(watchlist_id=name, name=name, created_at=ts, updated_at=ts)
