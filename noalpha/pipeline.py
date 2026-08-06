from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from noalpha.domain import AsOfContext, Evidence, EvidenceType, Instrument, ResearchReport, now_utc
from noalpha.errors import ProviderError, StockAgentError
from noalpha.event_detection import EventDetectionConfig, detect_events
from noalpha.evidence import normalize_all
from noalpha.pit import filter_evidence_as_of
from noalpha.provider_health import ProviderHealth, ProviderHealthStatus
from noalpha.providers import (
    FilingProvider,
    MarketDataAppPrototypeMarketDataProvider,
    MarketDataProvider,
    NewsProvider,
    SecFilingProvider,
    YahooFinanceNewsProvider,
    YahooPrototypeMarketDataProvider,
)
from noalpha.progress import (
    InvestigationStage, PersistentProgressReporter, ProgressReporter,
)
from noalpha.research import LLMResearchOrchestrator, ResearchContextBuilder, ResearchOrchestrator
from noalpha.research_mode import ResearchMode
from noalpha.llm.config import LLMConfig
from noalpha.llm.provider import OpenAICompatibleProvider
from noalpha.quality import canonicalize_evidence
from noalpha.storage import InMemoryEvidenceStore
from noalpha.timeline import build_timeline


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvestigationResult:
    investigation_id: str
    status: str
    report: ResearchReport
    evidence: tuple[Evidence, ...]
    provider_warnings: tuple[str, ...]
    quality_report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        def dt(value: datetime | None) -> str | None:
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None

        return {
            "investigation_id": self.investigation_id,
            "status": self.status,
            "report": self.report.to_dict(),
            "provider_warnings": list(self.provider_warnings),
            "quality_report": self.quality_report,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "evidence_type": item.evidence_type.value,
                    "source": item.source,
                    "source_url": item.source_url,
                    "event_time": dt(item.event_time),
                    "published_at": dt(item.published_at),
                    "available_at": dt(item.available_at),
                    "retrieved_at": dt(item.retrieved_at),
                    "content": item.content,
                    "metadata": item.metadata,
                }
                for item in self.evidence
            ],
        }


class InvestigationPipeline:
    def __init__(
        self,
        market_data: MarketDataProvider,
        filings: FilingProvider,
        news: NewsProvider,
        *,
        store: InMemoryEvidenceStore | None = None,
        research: ResearchOrchestrator | None = None,
        event_config: EventDetectionConfig | None = None,
    ) -> None:
        self.market_data = market_data
        self.filings = filings
        self.news = news
        self.store = store or InMemoryEvidenceStore()
        self.research = research or ResearchOrchestrator()
        self.event_config = event_config or EventDetectionConfig()

    @classmethod
    def default(cls, *, cache_dir: Path | None = None, marketdata_token: str | None = None,
                research_mode: ResearchMode = ResearchMode.DETERMINISTIC,
                llm_config: LLMConfig | None = None) -> "InvestigationPipeline":
        market_cache = cache_dir / "market" if cache_dir else None
        sec_cache = cache_dir / "sec" if cache_dir else None
        news_cache = cache_dir / "news" if cache_dir else None

        orchestrator: ResearchOrchestrator | LLMResearchOrchestrator = ResearchOrchestrator()
        if research_mode in (ResearchMode.LLM, ResearchMode.HYBRID) and llm_config and llm_config.api_key:
            provider = OpenAICompatibleProvider(llm_config)
            orchestrator = LLMResearchOrchestrator(provider=provider)

        return cls(
            market_data=MarketDataAppPrototypeMarketDataProvider(cache_dir=market_cache, token=marketdata_token),
            filings=SecFilingProvider(cache_dir=sec_cache),
            news=YahooFinanceNewsProvider(cache_dir=news_cache),
            research=orchestrator,
        )

    def investigate(
        self,
        symbol: str,
        *,
        as_of: AsOfContext | None = None,
        lookback_days: int = 90,
        filings_limit: int = 8,
        news_limit: int = 20,
        progress: ProgressReporter | None = None,
        report_language: str = "en-US",
    ) -> InvestigationResult:
        investigation_id = str(uuid4())
        context = as_of or AsOfContext.now()
        logger.info("investigation_started", extra={"investigation_id": investigation_id, "symbol": symbol})
        if progress:
            progress.stage_started(InvestigationStage.RESOLVING_INSTRUMENT, f"Resolving {symbol}")
        instrument = self.market_data.resolve_instrument(symbol)
        if progress:
            progress.stage_completed(InvestigationStage.RESOLVING_INSTRUMENT,
                f"Resolved: {instrument.symbol} → {instrument.company_name}",
                metrics={"symbol": instrument.symbol, "company_name": instrument.company_name})
        start = context.as_of - timedelta(days=lookback_days)
        end = context.as_of
        warnings: list[str] = []
        provider_health = self._initial_provider_health()

        bars = []
        filings = []
        facts = []
        news = []

        # ── Fetch Market Data ──
        if progress:
            progress.stage_started(InvestigationStage.FETCHING_MARKET_DATA, "Fetching daily price/volume bars")
        try:
            bars = self.market_data.get_bars(instrument, start, end)
            if progress:
                progress.stage_completed(InvestigationStage.FETCHING_MARKET_DATA,
                    f"{len(bars)} market bars fetched",
                    metrics={"market_bars": len(bars)})
        except ProviderError as exc:
            warnings.append(f"Market data provider failed: {exc}")
            provider_health["market_data"] = self._failure_health("market_data", exc, provider_health.get("market_data"))
            if progress:
                progress.stage_degraded(InvestigationStage.FETCHING_MARKET_DATA,
                    f"Market data unavailable: {exc}", metrics={"market_bars": 0})
                progress.set_provider_status("market_data", "degraded")
            logger.warning("market_provider_failed", extra={"investigation_id": investigation_id, "provider": "market_data"})

        # ── Fetch SEC Facts ──
        if progress:
            progress.stage_started(InvestigationStage.FETCHING_SEC_FACTS, "Fetching SEC company facts")
        try:
            facts = self.filings.get_company_facts(instrument)
            if progress:
                progress.stage_completed(InvestigationStage.FETCHING_SEC_FACTS,
                    f"{len(facts)} raw financial facts fetched",
                    metrics={"raw_financial_facts": len(facts)})
        except ProviderError as exc:
            warnings.append(f"Company facts provider failed: {exc}")
            provider_health["company_facts"] = self._failure_health("company_facts", exc, provider_health.get("company_facts"))
            if progress:
                progress.stage_degraded(InvestigationStage.FETCHING_SEC_FACTS,
                    f"SEC facts unavailable: {exc}", metrics={"raw_financial_facts": 0})

        # ── Fetch Filings ──
        if progress:
            progress.stage_started(InvestigationStage.FETCHING_FILINGS, "Fetching recent SEC filings")
        try:
            filings = self.filings.get_filings(instrument, limit=filings_limit)
            if progress:
                progress.stage_completed(InvestigationStage.FETCHING_FILINGS,
                    f"{len(filings)} filings fetched",
                    metrics={"filings": len(filings)})
        except ProviderError as exc:
            warnings.append(f"Filing provider failed: {exc}")
            provider_health["filings"] = self._failure_health("filings", exc, provider_health.get("filings"))
            if progress:
                progress.stage_degraded(InvestigationStage.FETCHING_FILINGS,
                    f"Filings unavailable: {exc}", metrics={"filings": 0})

        # ── Fetch News ──
        if progress:
            progress.stage_started(InvestigationStage.FETCHING_NEWS, "Searching recent news")
        try:
            news = self.news.search_news(instrument, start, end, limit=news_limit)
            if progress:
                progress.stage_completed(InvestigationStage.FETCHING_NEWS,
                    f"{len(news)} news items found",
                    metrics={"news_items": len(news)})
        except ProviderError as exc:
            warnings.append(f"News provider failed: {exc}")
            provider_health["news"] = self._failure_health("news", exc, provider_health.get("news"))
            if progress:
                progress.stage_degraded(InvestigationStage.FETCHING_NEWS,
                    f"News unavailable: {exc}", metrics={"news_items": 0})

        # ── Normalize ──
        if progress:
            progress.stage_started(InvestigationStage.NORMALIZING_EVIDENCE, "Converting to canonical evidence format")
        evidence = normalize_all(bars=bars, filings=filings, facts=facts, news=news)
        self.store.add_many(evidence)
        if progress:
            progress.stage_completed(InvestigationStage.NORMALIZING_EVIDENCE,
                f"{len(evidence)} raw evidence items normalized",
                metrics={"raw_evidence": len(evidence)})

        # ── PIT Filter ──
        if progress:
            progress.stage_started(InvestigationStage.APPLYING_PIT_FILTER, "Applying point-in-time filter")
        point_in_time = [
            item for item in filter_evidence_as_of(self.store.all(), context)
            if self._belongs_to_instrument(item, instrument)
        ]
        if progress:
            progress.stage_completed(InvestigationStage.APPLYING_PIT_FILTER,
                f"{len(point_in_time)} items passed PIT guard",
                metrics={"pit_filtered": len(point_in_time)})

        # ── Canonicalize + Deduplicate + Quality ──
        if progress:
            progress.stage_started(InvestigationStage.CANONICALIZING, "Canonicalizing and deduplicating evidence")
        usable, quality_report = canonicalize_evidence(point_in_time, context.as_of)
        quality_report["provider_health"] = {name: health.to_dict() for name, health in provider_health.items()}
        if progress:
            canonical = quality_report.get("canonical_evidence_count", len(usable))
            dups = quality_report.get("duplicate_count", 0)
            high_val = quality_report.get("high_value_evidence", len(usable))
            progress.stage_completed(InvestigationStage.CANONICALIZING,
                f"{canonical} canonical · {dups} duplicates removed · {high_val} high-value",
                metrics={
                    "canonical_evidence": canonical,
                    "duplicates_removed": dups,
                    "high_value_evidence": high_val,
                    "raw_evidence_count": quality_report.get("raw_evidence_count", 0),
                    "stale_facts_removed": quality_report.get("stale_financial_facts", 0),
                })

        # ── Timeline ──
        if progress:
            progress.stage_started(InvestigationStage.BUILDING_TIMELINE, "Building evidence timeline")
        timeline = build_timeline(usable)
        if progress:
            progress.stage_completed(InvestigationStage.BUILDING_TIMELINE,
                f"{len(timeline)} timeline events", metrics={"timeline_events": len(timeline)})

        # ── Event Detection ──
        if progress:
            progress.stage_started(InvestigationStage.DETECTING_EVENTS, "Running deterministic event detection")
        events = detect_events(instrument, usable, self.event_config)
        if progress:
            progress.stage_completed(InvestigationStage.DETECTING_EVENTS,
                f"{len(events)} events detected",
                metrics={"detected_events": len(events),
                         "event_types": [e.event_type.value for e in events]})

        if isinstance(self.research, LLMResearchOrchestrator):
            # ── Research Context ──
            unknown_types = []
            if EvidenceType.MARKET_BAR not in {item.evidence_type for item in usable}:
                unknown_types.append("Market data unavailable or filtered by as-of guard.")
            if EvidenceType.FINANCIAL_FACT not in {item.evidence_type for item in usable}:
                unknown_types.append("SEC company facts unavailable or filtered by as-of guard.")
            unknown_types.extend(str(w) for w in warnings)

            timeline_summary = " ".join(
                f"{t.timestamp.isoformat()}: {t.title}" for t in timeline[:10]
            )
            quality_summary = {
                "raw": quality_report.get("raw_evidence_count", 0),
                "canonical": quality_report.get("canonical_evidence_count", 0),
                "duplicates": quality_report.get("duplicate_count", 0),
            }
            if progress:
                progress.stage_started(InvestigationStage.BUILDING_RESEARCH_CONTEXT, "Selecting evidence for LLM context")
            ctx_builder = ResearchContextBuilder()
            research_ctx = ctx_builder.build(
                symbol=instrument.symbol,
                company_name=instrument.company_name,
                evidence=usable,
                events=events,
                timeline_summary=timeline_summary,
                quality_summary=quality_summary,
                unknowns=unknown_types,
                as_of=context.as_of,
            )
            if progress:
                progress.stage_completed(InvestigationStage.BUILDING_RESEARCH_CONTEXT,
                    f"{len(research_ctx.selected_evidence)} evidence selected for LLM context",
                    metrics={
                        "selected_evidence": len(research_ctx.selected_evidence),
                        "excluded_evidence": research_ctx.excluded_count,
                        "token_estimate": research_ctx.token_estimate,
                    })

            report = self.research.build_report(
                instrument, research_ctx, timeline, events, usable, warnings, quality_report,
                progress=progress, report_language=report_language,
            )
        else:
            report = self.research.build_report(
                instrument, context, usable, timeline, events, warnings, quality_report,
                report_language=report_language,
            )
        report = replace(report, investigation_id=investigation_id)
        quality_report = report.quality_report
        logger.info(
            "investigation_completed",
            extra={
                "investigation_id": investigation_id,
                "symbol": instrument.symbol,
                "evidence_count": len(usable),
            },
        )
        return InvestigationResult(
            investigation_id=investigation_id,
            status="completed",
            report=report,
            evidence=tuple(usable),
            provider_warnings=tuple(warnings),
            quality_report=quality_report,
        )

    def _belongs_to_instrument(self, evidence: Evidence, instrument: Instrument) -> bool:
        symbol = evidence.metadata.get("symbol")
        symbols = evidence.metadata.get("symbols")
        if symbol:
            return str(symbol).upper() == instrument.symbol
        if isinstance(symbols, (list, tuple, set)):
            return instrument.symbol in {str(item).upper() for item in symbols}
        return False

    def _initial_provider_health(self) -> dict[str, ProviderHealth]:
        health: dict[str, ProviderHealth] = {}
        if hasattr(self.market_data, "health"):
            health["market_data"] = self.market_data.health()
        if hasattr(self.filings, "health"):
            health["filings"] = self.filings.health()
            health["company_facts"] = self.filings.health()
        if hasattr(self.news, "health"):
            health["news"] = self.news.health()
        return health

    def _failure_health(self, name: str, exc: ProviderError, previous: ProviderHealth | None) -> ProviderHealth:
        message = str(exc)
        status = ProviderHealthStatus.UNAVAILABLE
        if "401" in message or "unauthorized" in message.lower() or "Invalid token" in message:
            status = ProviderHealthStatus.UNAUTHORIZED
        elif "rate" in message.lower() or "429" in message:
            status = ProviderHealthStatus.RATE_LIMITED
        return ProviderHealth(
            provider=previous.provider if previous else name,
            status=status,
            requires_credentials=previous.requires_credentials if previous else False,
            supports_historical=previous.supports_historical if previous else False,
            supports_realtime=previous.supports_realtime if previous else False,
            supports_intraday=previous.supports_intraday if previous else False,
            coverage=previous.coverage if previous else "unknown",
            reason=message,
        )
