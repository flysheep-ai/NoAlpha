from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from noalpha.confidence import calculate_confidence_breakdown
from noalpha.domain import AsOfContext, Claim, ClaimType, Evidence, EvidenceType, FinancialFact, Filing, Instrument, Market, MarketBar, NewsItem
from noalpha.errors import ProviderError
from noalpha.event_detection import EventDetectionConfig, detect_events
from noalpha.evidence import financial_fact_to_evidence, market_bar_to_evidence, news_item_to_evidence
from noalpha.pipeline import InvestigationPipeline
from noalpha.providers.base import FilingProvider, MarketDataProvider, NewsProvider
from noalpha.quality import canonicalize_evidence, comparable_period_key, economic_observation_id, evaluate_claim_citations


UTC = timezone.utc
INSTRUMENT = Instrument("AAPL", "NASDAQ", Market.US, "USD", "Apple Inc.", cik="0000320193")


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def fact(
    concept: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
    value: str = "100",
    *,
    start: str | None = "2026-01-01T00:00:00Z",
    end: str = "2026-03-31T00:00:00Z",
    filed: str = "2026-04-25T00:00:00Z",
    form: str = "10-Q",
    accession: str = "0000320193-26-000001",
    fp: str = "Q1",
    unit: str = "USD",
) -> Evidence:
    return financial_fact_to_evidence(
        FinancialFact(
            INSTRUMENT,
            concept,
            Decimal(value),
            unit,
            dt(start) if start else None,
            dt(end),
            dt(filed),
            None,
            "SEC EDGAR companyfacts",
            "https://sec.example/companyfacts",
            dt("2026-07-06T00:00:00Z"),
            form,
            fiscal_year=2026,
            fiscal_period=fp,
            accession_number=accession,
        )
    )


class UnauthorizedMarketProvider(MarketDataProvider):
    def resolve_instrument(self, symbol: str) -> Instrument:
        return INSTRUMENT

    def get_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        raise ProviderError("HTTP 401 from market provider")


class FutureMarketProvider(MarketDataProvider):
    def resolve_instrument(self, symbol: str) -> Instrument:
        return INSTRUMENT

    def get_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        return [
            MarketBar(INSTRUMENT, dt("2026-01-02T00:00:00Z"), Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 100, "test_market", dt("2026-01-03T00:00:00Z"), dt("2026-01-03T00:00:00Z")),
            MarketBar(INSTRUMENT, dt("2026-01-10T00:00:00Z"), Decimal("10"), Decimal("12"), Decimal("9"), Decimal("11"), 200, "test_market", dt("2026-01-11T00:00:00Z"), dt("2026-01-11T00:00:00Z")),
        ]


class EmptyFilingProvider(FilingProvider):
    def get_filings(self, instrument: Instrument, limit: int = 10) -> list[Filing]:
        return []

    def get_company_facts(self, instrument: Instrument) -> list[FinancialFact]:
        return []


class EmptyNewsProvider(NewsProvider):
    def search_news(self, instrument: Instrument, start: datetime, end: datetime, limit: int = 20) -> list[NewsItem]:
        return []


class QualityGateTests(unittest.TestCase):
    def test_economic_observation_identity_ignores_source_filing(self) -> None:
        original = fact(accession="0001", form="10-Q")
        repeated = fact(accession="0002", form="10-K")
        self.assertEqual(economic_observation_id(original), economic_observation_id(repeated))

    def test_financial_fact_dedup_and_amendment_handling(self) -> None:
        original = fact(value="100", accession="0001", form="10-K", filed="2026-04-25T00:00:00Z")
        amended = fact(value="101", accession="0002", form="10-K/A", filed="2026-05-01T00:00:00Z")
        canonical, report = canonicalize_evidence([original, amended], dt("2026-07-06T00:00:00Z"))
        self.assertEqual(report["financial_raw_count"], 2)
        self.assertEqual(report["financial_canonical_count"], 1)
        self.assertEqual(report["financial_duplicate_count"], 1)
        self.assertEqual(report["amended_observations"], 1)
        self.assertEqual(canonical[0].metadata["value"], "101")
        self.assertTrue(canonical[0].metadata["has_conflicting_disclosures"])

    def test_comparable_period_detection_separates_duration_buckets(self) -> None:
        quarter = fact(start="2026-01-01T00:00:00Z", end="2026-03-31T00:00:00Z", fp="Q1")
        annual = fact(start="2025-04-01T00:00:00Z", end="2026-03-31T00:00:00Z", fp="FY")
        canonical, _ = canonicalize_evidence([quarter, annual], dt("2026-07-06T00:00:00Z"))
        keys = {comparable_period_key(item) for item in canonical}
        self.assertEqual(len(keys), 2)

    def test_financial_change_requires_comparable_period_pair(self) -> None:
        q1 = fact(value="100", start="2026-01-01T00:00:00Z", end="2026-03-31T00:00:00Z", fp="Q1")
        annual = fact(value="200", start="2025-04-01T00:00:00Z", end="2026-03-31T00:00:00Z", fp="FY")
        canonical, _ = canonicalize_evidence([q1, annual], dt("2026-07-06T00:00:00Z"))
        events = detect_events(INSTRUMENT, canonical, EventDetectionConfig(financial_change_abs=Decimal("0.2")))
        self.assertFalse([event for event in events if event.event_type.value == "major_financial_change"])

    def test_confidence_penalizes_missing_market_data(self) -> None:
        evidence, report = canonicalize_evidence([fact()], dt("2026-07-06T00:00:00Z"))
        score, _, breakdown = calculate_confidence_breakdown(evidence, ["Market data unavailable"], dt("2026-07-06T00:00:00Z"), quality_report=report)
        self.assertLess(score, 0.75)
        self.assertLessEqual(breakdown["market_data"], 0.15)

    def test_duplicate_evidence_does_not_inflate_confidence(self) -> None:
        one = fact(accession="0001")
        duplicate = fact(accession="0002")
        canonical, duplicate_report = canonicalize_evidence([one, duplicate], dt("2026-07-06T00:00:00Z"))
        score_with_duplicates, _, breakdown = calculate_confidence_breakdown(canonical, [], dt("2026-07-06T00:00:00Z"), quality_report=duplicate_report)
        clean, clean_report = canonicalize_evidence([one], dt("2026-07-06T00:00:00Z"))
        score_clean, _, _ = calculate_confidence_breakdown(clean, [], dt("2026-07-06T00:00:00Z"), quality_report=clean_report)
        self.assertLess(breakdown["duplication"], 1.0)
        self.assertLessEqual(score_with_duplicates, score_clean)

    def test_claim_citation_support(self) -> None:
        evidence = [fact()]
        claim = Claim("C1", "Revenue was reported.", ClaimType.OBSERVATION, (evidence[0].evidence_id,), "direct", 0.9)
        missing = Claim("C2", "Unsupported.", ClaimType.OBSERVATION, ("missing",), "direct", 0.9)
        result = evaluate_claim_citations([claim, missing], evidence)
        self.assertEqual(result["supported_claims"], 1)
        self.assertEqual(result["unsupported_claims"], 1)
        self.assertEqual(result["citation_support_rate"], 0.5)

    def test_future_evidence_rejection_in_quality_gate(self) -> None:
        past = news_item_to_evidence(NewsItem("Past", dt("2026-01-01T00:00:00Z"), dt("2026-01-01T01:00:00Z"), "Test", "https://news/1", "Past", "en", ("AAPL",)))
        future = news_item_to_evidence(NewsItem("Future", dt("2026-01-05T00:00:00Z"), dt("2026-01-05T01:00:00Z"), "Test", "https://news/2", "Future", "en", ("AAPL",)))
        _, report = canonicalize_evidence([past, future], dt("2026-01-02T00:00:00Z"))
        self.assertEqual(report["future_evidence_rejected"], 1)

    def test_historical_replay_filters_future_market_bar(self) -> None:
        pipeline = InvestigationPipeline(FutureMarketProvider(), EmptyFilingProvider(), EmptyNewsProvider())
        result = pipeline.investigate("AAPL", as_of=AsOfContext(dt("2026-01-05T00:00:00Z")), lookback_days=10)
        self.assertEqual(len([item for item in result.evidence if item.evidence_type == EvidenceType.MARKET_BAR]), 1)
        self.assertTrue(all(item.available_at <= dt("2026-01-05T00:00:00Z") for item in result.evidence))

    def test_provider_unauthorized_health_and_confidence(self) -> None:
        pipeline = InvestigationPipeline(UnauthorizedMarketProvider(), EmptyFilingProvider(), EmptyNewsProvider())
        result = pipeline.investigate("AAPL", as_of=AsOfContext(dt("2026-01-05T00:00:00Z")), lookback_days=10)
        health = result.quality_report["provider_health"]["market_data"]
        self.assertEqual(health["status"], "unauthorized")
        self.assertLessEqual(result.report.confidence_breakdown["market_data"], 0.15)


if __name__ == "__main__":
    unittest.main()
