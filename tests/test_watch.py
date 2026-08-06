from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from noalpha.domain import AsOfContext, FinancialFact, Filing, Instrument, Market, MarketBar, NewsItem
from noalpha.errors import ProviderError
from noalpha.pipeline import InvestigationPipeline
from noalpha.repositories import SQLiteRepository
from noalpha.watch import AutonomousWatchService, aggregate_triggers, candidate_from_event
from noalpha.watch_models import ScanPolicy, TriggerType
from noalpha.providers.base import FilingProvider, MarketDataProvider, NewsProvider
from noalpha.event_detection import EventDetectionConfig, detect_events
from noalpha.evidence import normalize_all


UTC = timezone.utc
INSTRUMENT = Instrument("AAPL", "NASDAQ", Market.US, "USD", "Apple Inc.", cik="0000320193")


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class ScenarioMarketProvider(MarketDataProvider):
    def __init__(self, *, fail: bool = False, future: bool = False) -> None:
        self.fail = fail
        self.future = future

    def resolve_instrument(self, symbol: str) -> Instrument:
        return INSTRUMENT

    def get_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        if self.fail:
            raise ProviderError("HTTP 401 from market provider")
        observed = dt("2026-07-06T00:00:00Z")
        bars = [
            MarketBar(instrument, dt("2026-07-01T00:00:00Z"), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), 1000, "test_market", observed, dt("2026-07-02T00:00:00Z")),
            MarketBar(instrument, dt("2026-07-02T00:00:00Z"), Decimal("100"), Decimal("114"), Decimal("99"), Decimal("113"), 5000, "test_market", observed, dt("2026-07-03T00:00:00Z")),
        ]
        if self.future:
            bars.append(MarketBar(instrument, dt("2026-07-08T00:00:00Z"), Decimal("113"), Decimal("120"), Decimal("110"), Decimal("119"), 9000, "test_market", observed, dt("2026-07-09T00:00:00Z")))
        return bars


class ScenarioFilingProvider(FilingProvider):
    def __init__(self, *, filing: bool = False) -> None:
        self.filing = filing

    def get_filings(self, instrument: Instrument, limit: int = 10) -> list[Filing]:
        if not self.filing:
            return []
        return [
            Filing(
                instrument,
                "8-K",
                dt("2026-07-04T00:00:00Z"),
                dt("2026-07-04T12:00:00Z"),
                dt("2026-07-04T00:00:00Z"),
                "https://sec.example/8k",
                "SEC EDGAR submissions",
                dt("2026-07-06T00:00:00Z"),
                "hash",
                "0000320193-26-000100",
            )
        ]

    def get_company_facts(self, instrument: Instrument) -> list[FinancialFact]:
        return [
            FinancialFact(
                instrument,
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                Decimal("1000000000"),
                "USD",
                dt("2026-04-01T00:00:00Z"),
                dt("2026-06-30T00:00:00Z"),
                dt("2026-07-04T00:00:00Z"),
                None,
                "SEC EDGAR companyfacts",
                "https://sec.example/facts",
                dt("2026-07-06T00:00:00Z"),
                "10-Q",
                fiscal_year=2026,
                fiscal_period="Q2",
                accession_number="0000320193-26-000101",
            )
        ]


class ScenarioNewsProvider(NewsProvider):
    def __init__(self, *, burst: bool = True) -> None:
        self.burst = burst

    def search_news(self, instrument: Instrument, start: datetime, end: datetime, limit: int = 20) -> list[NewsItem]:
        if not self.burst:
            return []
        return [
            NewsItem(f"Apple news {idx}", dt(f"2026-07-03T1{idx}:00:00Z"), dt("2026-07-06T00:00:00Z"), "Test News", f"https://news.example/{idx}", "News", "en", ("AAPL",))
            for idx in range(3)
        ]


def service_for(repo: SQLiteRepository, market: MarketDataProvider, filings: FilingProvider, news: NewsProvider) -> AutonomousWatchService:
    return AutonomousWatchService(repo, market, filings, news, InvestigationPipeline(market, filings, news))


class WatchTests(unittest.TestCase):
    def repo(self) -> SQLiteRepository:
        self.tmp = tempfile.TemporaryDirectory()
        return SQLiteRepository(Path(self.tmp.name) / "watch.sqlite3")

    def add_item(self, repo: SQLiteRepository, policy: ScanPolicy | None = None) -> None:
        watchlist = repo.create_watchlist("default")
        repo.add_watch_item(watchlist, INSTRUMENT, policy or ScanPolicy(scan_interval_seconds=0, cooldown_seconds=3600), enabled=True)

    def test_watchlist_crud_persists_items(self) -> None:
        repo = self.repo()
        self.add_item(repo)
        watchlist = repo.get_watchlist("default")
        self.assertIsNotNone(watchlist)
        items = repo.list_watch_items(watchlist.watchlist_id)
        self.assertEqual([item.instrument.symbol for item in items], ["AAPL"])
        repo.close()

    def test_trigger_candidate_aggregation_and_scoring(self) -> None:
        policy = ScanPolicy(scan_interval_seconds=0, cooldown_seconds=3600)
        evidence = normalize_all(bars=ScenarioMarketProvider().get_bars(INSTRUMENT, dt("2026-07-01T00:00:00Z"), dt("2026-07-06T00:00:00Z")))
        events = detect_events(INSTRUMENT, evidence, EventDetectionConfig(abnormal_return_abs=Decimal("0.06"), abnormal_volume_ratio=Decimal("2.5")))
        candidates = [candidate_from_event(INSTRUMENT, event, dt("2026-07-06T00:00:00Z"), policy) for event in events]
        clusters = aggregate_triggers(candidates, policy)
        self.assertTrue(candidates)
        self.assertTrue(clusters)
        self.assertGreaterEqual(clusters[0].score, policy.investigation_score_threshold)
        self.assertIn(TriggerType.ABNORMAL_RETURN, clusters[0].trigger_types)

    def test_run_once_creates_case_and_snapshot(self) -> None:
        repo = self.repo()
        self.add_item(repo)
        summary = service_for(repo, ScenarioMarketProvider(), ScenarioFilingProvider(), ScenarioNewsProvider()).run_once(as_of=AsOfContext(dt("2026-07-06T00:00:00Z")))
        self.assertEqual(summary.cases_created, 1)
        cases = repo.list_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].status.value, "OPEN")
        self.assertEqual(len(repo.list_snapshots(cases[0].case_id)), 1)
        repo.close()

    def test_second_scan_prevents_duplicate_case_with_cooldown(self) -> None:
        repo = self.repo()
        self.add_item(repo)
        service = service_for(repo, ScenarioMarketProvider(), ScenarioFilingProvider(), ScenarioNewsProvider())
        first = service.run_once(as_of=AsOfContext(dt("2026-07-06T00:00:00Z")))
        second = service.run_once(as_of=AsOfContext(dt("2026-07-06T00:05:00Z")))
        self.assertEqual(first.cases_created, 1)
        self.assertEqual(second.cases_created, 0)
        self.assertGreaterEqual(second.cooldown_suppressed, 1)
        self.assertEqual(len(repo.list_cases()), 1)
        repo.close()

    def test_material_new_filing_bypasses_cooldown_and_updates_case(self) -> None:
        repo = self.repo()
        self.add_item(repo)
        service = service_for(repo, ScenarioMarketProvider(), ScenarioFilingProvider(filing=False), ScenarioNewsProvider())
        service.run_once(as_of=AsOfContext(dt("2026-07-06T00:00:00Z")))
        escalated = service_for(repo, ScenarioMarketProvider(), ScenarioFilingProvider(filing=True), ScenarioNewsProvider())
        summary = escalated.run_once(as_of=AsOfContext(dt("2026-07-06T00:10:00Z")))
        self.assertEqual(summary.cases_updated, 1)
        self.assertEqual(summary.duplicate_cases_prevented, 1)
        case = repo.list_cases()[0]
        self.assertIn(case.status.value, {"UPDATED", "MONITORING"})
        self.assertGreaterEqual(len(repo.list_snapshots(case.case_id)), 2)
        repo.close()

    def test_provider_degraded_mode_does_not_crash_or_create_market_trigger(self) -> None:
        repo = self.repo()
        self.add_item(repo)
        summary = service_for(repo, ScenarioMarketProvider(fail=True), ScenarioFilingProvider(), ScenarioNewsProvider(burst=False)).run_once(as_of=AsOfContext(dt("2026-07-06T00:00:00Z")))
        self.assertEqual(summary.provider_failures, 1)
        self.assertEqual(summary.cases_created, 0)
        self.assertIn("AAPL", summary.degraded_symbols)
        repo.close()

    def test_watch_scan_rejects_future_evidence(self) -> None:
        repo = self.repo()
        self.add_item(repo)
        service = service_for(repo, ScenarioMarketProvider(future=True), ScenarioFilingProvider(), ScenarioNewsProvider(burst=False))
        scan = service.scan_item(repo.list_watch_items(repo.get_watchlist("default").watchlist_id)[0], AsOfContext(dt("2026-07-06T00:00:00Z")))
        self.assertTrue(scan["evidence"])
        self.assertTrue(all(item.available_at <= dt("2026-07-06T00:00:00Z") for item in scan["evidence"]))
        repo.close()


if __name__ == "__main__":
    unittest.main()
