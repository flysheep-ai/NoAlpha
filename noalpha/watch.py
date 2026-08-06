from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from noalpha.domain import AsOfContext, DetectedEvent, Evidence, EventType, Instrument, now_utc, stable_id
from noalpha.errors import ProviderError
from noalpha.event_detection import EventDetectionConfig, detect_events
from noalpha.evidence import normalize_all
from noalpha.pipeline import InvestigationPipeline
from noalpha.provider_health import ProviderHealth, ProviderHealthStatus
from noalpha.providers import FilingProvider, MarketDataAppPrototypeMarketDataProvider, MarketDataProvider, NewsProvider, SecFilingProvider, YahooFinanceNewsProvider
from noalpha.repositories import SQLiteRepository
from noalpha.watch_models import (
    CaseStatus,
    InvestigationCase,
    ScanPolicy,
    ThesisSnapshot,
    TriggerCandidate,
    TriggerCluster,
    TriggerType,
    WatchDecision,
    WatchItem,
    WatchScanSummary,
)


logger = logging.getLogger(__name__)


class AutonomousWatchService:
    def __init__(
        self,
        repo: SQLiteRepository,
        market_data: MarketDataProvider,
        filings: FilingProvider,
        news: NewsProvider,
        investigation_pipeline: InvestigationPipeline,
    ) -> None:
        self.repo = repo
        self.market_data = market_data
        self.filings = filings
        self.news = news
        self.investigation_pipeline = investigation_pipeline

    @classmethod
    def default(cls, *, db_path: Path, cache_dir: Path | None = None) -> "AutonomousWatchService":
        repo = SQLiteRepository(db_path)
        market_cache = cache_dir / "market" if cache_dir else None
        sec_cache = cache_dir / "sec" if cache_dir else None
        news_cache = cache_dir / "news" if cache_dir else None
        market = MarketDataAppPrototypeMarketDataProvider(cache_dir=market_cache)
        filings = SecFilingProvider(cache_dir=sec_cache)
        news = YahooFinanceNewsProvider(cache_dir=news_cache)
        return cls(repo, market, filings, news, InvestigationPipeline(market, filings, news))

    def run_once(self, watchlist_name: str = "default", *, as_of: AsOfContext | None = None) -> WatchScanSummary:
        context = as_of or AsOfContext.now()
        watchlist = self.repo.get_watchlist(watchlist_name) or self.repo.create_watchlist(watchlist_name)
        items = [item for item in self.repo.list_watch_items(watchlist.watchlist_id) if item.enabled]
        scan_id = str(uuid4())
        candidates: list[TriggerCandidate] = []
        clusters: list[TriggerCluster] = []
        investigations_started = 0
        cases_created = 0
        cases_updated = 0
        duplicate_cases_prevented = 0
        cooldown_suppressed = 0
        provider_failures = 0
        degraded_symbols: set[str] = set()
        case_ids: list[str] = []

        for item in items:
            if not self._scan_due(item, context.as_of):
                continue
            scan = self.scan_item(item, context)
            candidates.extend(scan["candidates"])
            provider_failures += int(scan["provider_failures"])
            if scan["degraded"]:
                degraded_symbols.add(item.instrument.symbol)
            item_clusters = aggregate_triggers(scan["candidates"], item.scan_policy)
            clusters.extend(item_clusters)
            for cluster in sorted(item_clusters, key=lambda value: value.score, reverse=True):
                decision = self._decide(item, cluster, context.as_of)
                if decision == WatchDecision.COOLDOWN_SUPPRESSED:
                    cooldown_suppressed += 1
                    continue
                if decision == WatchDecision.IGNORE:
                    continue
                investigations_started += 1
                case, created, duplicate_prevented = self._investigate_case(item, cluster, context)
                case_ids.append(case.case_id)
                cases_created += int(created)
                cases_updated += int(not created)
                duplicate_cases_prevented += int(duplicate_prevented)
                break
            cooldown_until = context.as_of + item.scan_policy.cooldown if item_clusters else item.cooldown_until
            self.repo.update_watch_item_scan(item, context.as_of, cooldown_until)

        return WatchScanSummary(
            scan_id=scan_id,
            watchlist_id=watchlist.watchlist_id,
            scanned_at=context.as_of,
            scan_count=len(items),
            trigger_candidates=len(candidates),
            trigger_clusters=len(clusters),
            investigations_started=investigations_started,
            cases_created=cases_created,
            cases_updated=cases_updated,
            duplicate_cases_prevented=duplicate_cases_prevented,
            cooldown_suppressed=cooldown_suppressed,
            provider_failures=provider_failures,
            degraded_symbols=tuple(sorted(degraded_symbols)),
            case_ids=tuple(case_ids),
        )

    def scan_item(self, item: WatchItem, context: AsOfContext) -> dict[str, Any]:
        policy = item.scan_policy
        start = context.as_of - timedelta(days=policy.lookback_days)
        bars = []
        filings = []
        news = []
        failures = 0
        degraded = False
        provider_health: dict[str, ProviderHealth] = {}

        try:
            bars = self.market_data.get_bars(item.instrument, start, context.as_of)
        except ProviderError as exc:
            failures += 1
            degraded = True
            provider_health["market_data"] = _failure_health("market_data", exc)
            logger.warning("watch_market_provider_failed", extra={"symbol": item.instrument.symbol})
        try:
            if policy.filing_trigger_enabled:
                filings = self.filings.get_filings(item.instrument, limit=5)
        except ProviderError as exc:
            failures += 1
            degraded = True
            provider_health["filings"] = _failure_health("filings", exc)
        try:
            news = self.news.search_news(item.instrument, start, context.as_of, limit=20)
        except ProviderError as exc:
            failures += 1
            degraded = True
            provider_health["news"] = _failure_health("news", exc)

        evidence = [
            item_evidence
            for item_evidence in normalize_all(bars=bars, filings=filings, news=news)
            if item_evidence.available_at <= context.as_of
        ]
        config = EventDetectionConfig(
            abnormal_return_abs=Decimal(str(policy.return_threshold)),
            abnormal_volume_ratio=Decimal(str(policy.volume_threshold)),
            news_burst_count=policy.news_burst_threshold,
        )
        events = detect_events(item.instrument, evidence, config)
        candidates = [candidate_from_event(item.instrument, event, context.as_of, policy) for event in events if event.event_type in EVENT_TO_TRIGGER]
        candidates.extend(provider_health_candidates(item.instrument, context.as_of, provider_health, policy))
        return {"evidence": evidence, "events": events, "candidates": candidates, "provider_failures": failures, "degraded": degraded}

    def _scan_due(self, item: WatchItem, as_of: datetime) -> bool:
        if item.last_scanned_at is None:
            return True
        return item.last_scanned_at + timedelta(seconds=item.scan_policy.scan_interval_seconds) <= as_of

    def _decide(self, item: WatchItem, cluster: TriggerCluster, as_of: datetime) -> WatchDecision:
        if cluster.score < item.scan_policy.investigation_score_threshold:
            return WatchDecision.IGNORE
        existing = self.repo.find_open_case(item.instrument.symbol)
        if existing and existing.cooldown_until and existing.cooldown_until > as_of and not is_material_escalation(cluster, existing):
            return WatchDecision.COOLDOWN_SUPPRESSED
        if item.cooldown_until and item.cooldown_until > as_of and not is_material_escalation(cluster, existing):
            return WatchDecision.COOLDOWN_SUPPRESSED
        return WatchDecision.UPDATE_CASE if existing else WatchDecision.CREATE_CASE

    def _investigate_case(self, item: WatchItem, cluster: TriggerCluster, context: AsOfContext) -> tuple[InvestigationCase, bool, bool]:
        existing = self.repo.find_open_case(item.instrument.symbol)
        result = self.investigation_pipeline.investigate(
            item.instrument.symbol,
            as_of=context,
            lookback_days=max(30, item.scan_policy.lookback_days),
        )
        report = result.report
        now = context.as_of
        cooldown_until = now + item.scan_policy.cooldown
        if existing:
            thesis_change = thesis_diff(existing, report.to_dict())
            merged_clusters = tuple(dict.fromkeys((*existing.trigger_cluster_ids, cluster.cluster_id)))
            merged_evidence = tuple(dict.fromkeys((*existing.evidence_ids, *(item.evidence_id for item in result.evidence))))
            updated = replace(
                existing,
                status=CaseStatus.UPDATED if thesis_change != "unchanged" else CaseStatus.MONITORING,
                updated_at=now,
                trigger_cluster_ids=merged_clusters,
                primary_trigger=_cluster_trigger_key(cluster) or existing.primary_trigger,
                severity=max(existing.severity, cluster.severity),
                current_thesis=report.summary,
                counter_thesis=" | ".join(report.bear_case[:2]),
                unknowns=tuple(report.unknowns),
                confidence=report.confidence,
                latest_report_id=report.investigation_id,
                evidence_ids=merged_evidence,
                cooldown_until=cooldown_until,
            )
            self.repo.update_case(updated)
            self.repo.add_snapshot(snapshot_from_report(updated.case_id, report.to_dict(), thesis_change))
            return updated, False, True

        case = InvestigationCase(
            case_id=stable_id("case", item.instrument.symbol, cluster.cluster_id, now.isoformat()),
            instrument=item.instrument,
            status=CaseStatus.OPEN,
            opened_at=now,
            updated_at=now,
            closed_at=None,
            trigger_cluster_ids=(cluster.cluster_id,),
            primary_trigger=_cluster_trigger_key(cluster) or "unknown",
            severity=cluster.severity,
            current_thesis=report.summary,
            counter_thesis=" | ".join(report.bear_case[:2]),
            unknowns=tuple(report.unknowns),
            confidence=report.confidence,
            latest_report_id=report.investigation_id,
            evidence_ids=tuple(item.evidence_id for item in result.evidence),
            cooldown_until=cooldown_until,
        )
        self.repo.create_case(case)
        self.repo.add_snapshot(snapshot_from_report(case.case_id, report.to_dict(), "initial"))
        return case, True, False


def candidate_from_event(instrument: Instrument, event: DetectedEvent, detected_at: datetime, policy: ScanPolicy) -> TriggerCandidate:
    trigger_type = EVENT_TO_TRIGGER[event.event_type]
    base = policy.weights.get(trigger_type.value, 0.2)
    score = min(1.0, base * max(1.0, event.severity))
    return TriggerCandidate(
        trigger_id=stable_id("trigger", instrument.symbol, trigger_type.value, event.event_id),
        instrument=instrument,
        trigger_type=trigger_type,
        detected_at=detected_at,
        event_time=event.event_time,
        severity=event.severity,
        score=score,
        evidence_ids=event.evidence_refs,
        metadata={"event_id": event.event_id, "description": event.description, **event.metadata},
    )


def provider_health_candidates(
    instrument: Instrument,
    detected_at: datetime,
    health: dict[str, ProviderHealth],
    policy: ScanPolicy,
) -> list[TriggerCandidate]:
    candidates: list[TriggerCandidate] = []
    for provider, item in health.items():
        if item.status not in {ProviderHealthStatus.UNAUTHORIZED, ProviderHealthStatus.RATE_LIMITED, ProviderHealthStatus.UNAVAILABLE}:
            continue
        trigger_type = TriggerType.PROVIDER_HEALTH_CHANGE
        candidates.append(
            TriggerCandidate(
                trigger_id=stable_id("trigger", instrument.symbol, provider, item.status.value, detected_at.isoformat()),
                instrument=instrument,
                trigger_type=trigger_type,
                detected_at=detected_at,
                event_time=detected_at,
                severity=0.4 if item.status == ProviderHealthStatus.UNAUTHORIZED else 0.3,
                score=policy.weights.get(trigger_type.value, 0.2),
                evidence_ids=(),
                metadata={"provider": provider, "status": item.status.value, "reason": item.reason},
            )
        )
    return candidates


def aggregate_triggers(candidates: list[TriggerCandidate], policy: ScanPolicy) -> list[TriggerCluster]:
    by_symbol: dict[str, list[TriggerCandidate]] = {}
    for candidate in candidates:
        by_symbol.setdefault(candidate.instrument.symbol, []).append(candidate)
    clusters: list[TriggerCluster] = []
    for symbol, symbol_candidates in by_symbol.items():
        symbol_candidates.sort(key=lambda item: item.event_time)
        current: list[TriggerCandidate] = []
        for candidate in symbol_candidates:
            if not current or candidate.event_time - current[0].event_time <= policy.cluster_window:
                current.append(candidate)
                continue
            clusters.append(score_cluster(current, policy))
            current = [candidate]
        if current:
            clusters.append(score_cluster(current, policy))
    return clusters


def score_cluster(candidates: list[TriggerCandidate], policy: ScanPolicy) -> TriggerCluster:
    instrument = candidates[0].instrument
    trigger_types = tuple(sorted({candidate.trigger_type for candidate in candidates}, key=lambda item: item.value))
    severity = max(candidate.severity for candidate in candidates)
    diversity_bonus = 0.12 * max(0, len(trigger_types) - 1)
    score = min(1.0, sum(candidate.score for candidate in candidates) + diversity_bonus)
    evidence_ids = tuple(dict.fromkeys(ref for candidate in candidates for ref in candidate.evidence_ids))
    return TriggerCluster(
        cluster_id=stable_id("cluster", instrument.symbol, candidates[0].event_time.isoformat(), tuple(t.value for t in trigger_types)),
        instrument=instrument,
        window_start=min(candidate.event_time for candidate in candidates),
        window_end=max(candidate.event_time for candidate in candidates),
        trigger_types=trigger_types,
        severity=severity,
        score=score,
        trigger_ids=tuple(candidate.trigger_id for candidate in candidates),
        evidence_ids=evidence_ids,
        metadata={"trigger_count": len(candidates)},
    )


def is_material_escalation(cluster: TriggerCluster, existing: InvestigationCase | None) -> bool:
    if existing is None:
        return True
    existing_types = set(existing.primary_trigger.split(","))
    cluster_types = {item.value for item in cluster.trigger_types}
    if TriggerType.NEW_FILING.value in cluster_types:
        return True
    if not cluster_types.issubset(existing_types):
        return True
    return cluster.severity >= existing.severity + 0.25


def _cluster_trigger_key(cluster: TriggerCluster) -> str:
    return ",".join(item.value for item in cluster.trigger_types)


def snapshot_from_report(case_id: str, report: dict[str, Any], thesis_change: str) -> ThesisSnapshot:
    return ThesisSnapshot(
        snapshot_id=stable_id("snapshot", case_id, report["investigation_id"], report["generated_at"]),
        case_id=case_id,
        created_at=datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00")),
        summary=report["summary"],
        bull_case=tuple(report.get("bull_case", [])),
        bear_case=tuple(report.get("bear_case", [])),
        risks=tuple(report.get("risks", [])),
        unknowns=tuple(report.get("unknowns", [])),
        confidence=float(report["confidence"]),
        evidence_ids=tuple(report.get("evidence_refs", [])),
        report=report,
        thesis_change=thesis_change,
    )


def thesis_diff(existing: InvestigationCase, report: dict[str, Any]) -> str:
    new_confidence = float(report["confidence"])
    if new_confidence >= existing.confidence + 0.08:
        return "strengthened"
    if new_confidence <= existing.confidence - 0.08:
        return "weakened"
    old_unknowns = set(existing.unknowns)
    new_unknowns = set(report.get("unknowns", []))
    if len(new_unknowns - old_unknowns) >= 2:
        return "weakened"
    old_text = existing.current_thesis.lower()
    new_text = str(report.get("summary", "")).lower()
    if old_text and new_text and old_text != new_text:
        return "updated"
    return "unchanged"


def _failure_health(provider: str, exc: ProviderError) -> ProviderHealth:
    message = str(exc)
    status = ProviderHealthStatus.UNAVAILABLE
    if "401" in message or "unauthorized" in message.lower() or "invalid token" in message.lower():
        status = ProviderHealthStatus.UNAUTHORIZED
    elif "429" in message or "rate" in message.lower():
        status = ProviderHealthStatus.RATE_LIMITED
    return ProviderHealth(
        provider=provider,
        status=status,
        requires_credentials=status == ProviderHealthStatus.UNAUTHORIZED,
        supports_historical=False,
        supports_realtime=False,
        supports_intraday=False,
        coverage="unknown",
        reason=message,
    )


EVENT_TO_TRIGGER = {
    EventType.ABNORMAL_RETURN: TriggerType.ABNORMAL_RETURN,
    EventType.ABNORMAL_VOLUME: TriggerType.ABNORMAL_VOLUME,
    EventType.NEW_FILING: TriggerType.NEW_FILING,
    EventType.NEWS_BURST: TriggerType.NEWS_BURST,
}
