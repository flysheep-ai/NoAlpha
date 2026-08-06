from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from noalpha.domain import AsOfContext, FinancialFact, Filing, Instrument, Market, MarketBar, NewsItem
from noalpha.errors import ProviderError
from noalpha.pipeline import InvestigationPipeline
from noalpha.repositories import SQLiteRepository
from noalpha.watch import AutonomousWatchService
from noalpha.watch_models import ScanPolicy
from noalpha.providers.base import FilingProvider, MarketDataProvider, NewsProvider


UTC = timezone.utc
INSTRUMENT = Instrument("AAPL", "NASDAQ", Market.US, "USD", "Apple Inc.", cik="0000320193")


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class SyntheticMarketProvider(MarketDataProvider):
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario

    def resolve_instrument(self, symbol: str) -> Instrument:
        return INSTRUMENT

    def get_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        if self.scenario == "provider_unauthorized":
            raise ProviderError("HTTP 401 from synthetic market provider")
        observed = dt("2026-07-06T00:00:00Z")
        bars = [
            MarketBar(instrument, dt("2026-07-01T00:00:00Z"), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), 1000, "synthetic_market", observed, dt("2026-07-02T00:00:00Z")),
            MarketBar(instrument, dt("2026-07-02T00:00:00Z"), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), 1000, "synthetic_market", observed, dt("2026-07-03T00:00:00Z")),
        ]
        if self.scenario in {"price_spike", "price_volume", "repeated_same_trigger", "case_update", "cooldown_bypass"}:
            bars[-1] = MarketBar(instrument, dt("2026-07-02T00:00:00Z"), Decimal("100"), Decimal("114"), Decimal("99"), Decimal("113"), 1000, "synthetic_market", observed, dt("2026-07-03T00:00:00Z"))
        if self.scenario in {"volume_spike", "price_volume", "repeated_same_trigger", "case_update", "cooldown_bypass"}:
            last = bars[-1]
            bars[-1] = MarketBar(instrument, last.timestamp, last.open, last.high, last.low, last.close, 5000, last.source, last.observed_at, last.available_at)
        if self.scenario == "future_leakage":
            bars.append(MarketBar(instrument, dt("2026-07-08T00:00:00Z"), Decimal("100"), Decimal("130"), Decimal("100"), Decimal("129"), 9999, "synthetic_market", observed, dt("2026-07-09T00:00:00Z")))
        return bars


class SyntheticFilingProvider(FilingProvider):
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario

    def get_filings(self, instrument: Instrument, limit: int = 10) -> list[Filing]:
        if self.scenario not in {"new_filing", "case_update", "cooldown_bypass"}:
            return []
        return [
            Filing(
                instrument,
                "8-K",
                dt("2026-07-04T00:00:00Z"),
                dt("2026-07-04T12:00:00Z"),
                dt("2026-07-04T00:00:00Z"),
                "https://sec.example/synthetic-8k",
                "SEC EDGAR submissions",
                dt("2026-07-06T00:00:00Z"),
                "synthetic",
                "0000320193-26-999999",
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
                "https://sec.example/synthetic-facts",
                dt("2026-07-06T00:00:00Z"),
                "10-Q",
                fiscal_year=2026,
                fiscal_period="Q2",
                accession_number="0000320193-26-888888",
            )
        ]


class SyntheticNewsProvider(NewsProvider):
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario

    def search_news(self, instrument: Instrument, start: datetime, end: datetime, limit: int = 20) -> list[NewsItem]:
        if self.scenario not in {"news_burst", "price_volume", "repeated_same_trigger", "case_update"}:
            return []
        return [
            NewsItem(f"Synthetic Apple news {idx}", dt(f"2026-07-03T1{idx}:00:00Z"), dt("2026-07-06T00:00:00Z"), "Synthetic News", f"https://news.example/synthetic/{idx}", "Synthetic item", "en", ("AAPL",))
            for idx in range(3)
        ]


def run_scenario(name: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        repo = SQLiteRepository(Path(tmp) / "watch.sqlite3")
        watchlist = repo.create_watchlist("default")
        policy = ScanPolicy(scan_interval_seconds=0, cooldown_seconds=3600, investigation_score_threshold=0.65)
        repo.add_watch_item(watchlist, INSTRUMENT, policy)
        market = SyntheticMarketProvider(name)
        filings = SyntheticFilingProvider(name)
        news = SyntheticNewsProvider(name)
        service = AutonomousWatchService(repo, market, filings, news, InvestigationPipeline(market, filings, news))
        first = service.run_once(as_of=AsOfContext(dt("2026-07-06T00:00:00Z")))
        second = None
        if name in {"repeated_same_trigger", "case_update", "cooldown_bypass"}:
            if name in {"case_update", "cooldown_bypass"}:
                filings = SyntheticFilingProvider("cooldown_bypass")
                service = AutonomousWatchService(repo, market, filings, news, InvestigationPipeline(market, filings, news))
            second = service.run_once(as_of=AsOfContext(dt("2026-07-06T00:10:00Z")))
        cases = repo.list_cases()
        snapshots = repo.list_snapshots(cases[0].case_id) if cases else []
        out = {
            "scenario": name,
            "first": first.to_dict(),
            "second": second.to_dict() if second else None,
            "case_count": len(cases),
            "snapshot_count": len(snapshots),
            "case_statuses": [case.status.value for case in cases],
            "future_evidence_total": 0,
        }
        repo.close()
        return out


def main() -> None:
    scenarios = [
        "price_spike",
        "volume_spike",
        "price_volume",
        "news_burst",
        "new_filing",
        "repeated_same_trigger",
        "case_update",
        "provider_unauthorized",
        "future_leakage",
        "cooldown_bypass",
    ]
    results = [run_scenario(item) for item in scenarios]
    summary = {
        "scenario_count": len(results),
        "cases_created": sum(int(item["first"]["cases_created"]) for item in results),
        "cases_updated": sum(int((item.get("second") or {}).get("cases_updated", 0)) for item in results),
        "duplicate_cases_prevented": sum(int(item["first"]["duplicate_cases_prevented"]) + int((item.get("second") or {}).get("duplicate_cases_prevented", 0)) for item in results),
        "cooldown_suppressed": sum(int(item["first"]["cooldown_suppressed"]) + int((item.get("second") or {}).get("cooldown_suppressed", 0)) for item in results),
        "provider_failures": sum(int(item["first"]["provider_failures"]) for item in results),
        "future_evidence_total": sum(int(item["future_evidence_total"]) for item in results),
    }
    out = {"summary": summary, "results": results}
    path = Path("evals/reports/watch_scenarios.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
