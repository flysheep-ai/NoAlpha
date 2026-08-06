from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from noalpha.confidence import calculate_confidence
from noalpha.domain import (
    AsOfContext,
    Evidence,
    EvidenceType,
    FinancialFact,
    Filing,
    Instrument,
    Market,
    MarketBar,
    NewsItem,
)
from noalpha.errors import MalformedResponseError, ProviderTimeoutError, PointInTimeViolation
from noalpha.event_detection import EventDetectionConfig, detect_events
from noalpha.evidence import dedupe_evidence, financial_fact_to_evidence, normalize_all
from noalpha.http import HttpResponse
from noalpha.pipeline import InvestigationPipeline
from noalpha.pit import assert_available_as_of, filter_evidence_as_of
from noalpha.providers.marketdata_app import MarketDataAppPrototypeMarketDataProvider
from noalpha.providers.base import FilingProvider, MarketDataProvider, NewsProvider
from noalpha.timeline import build_timeline


UTC = timezone.utc


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


INSTRUMENT = Instrument("NVDA", "NASDAQ", Market.US, "USD", "NVIDIA Corporation", cik="0001045810")


class FakeMarketProvider(MarketDataProvider):
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def resolve_instrument(self, symbol: str) -> Instrument:
        return INSTRUMENT

    def get_bars(self, instrument: Instrument, start: datetime, end: datetime) -> list[MarketBar]:
        if self.fail:
            raise ProviderTimeoutError("market timeout")
        observed = dt("2026-07-06T00:00:00Z")
        return [
            MarketBar(instrument, dt("2026-07-01T00:00:00Z"), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), 1000, "test_market", observed, dt("2026-07-02T00:00:00Z")),
            MarketBar(instrument, dt("2026-07-02T00:00:00Z"), Decimal("100"), Decimal("112"), Decimal("99"), Decimal("111"), 5000, "test_market", observed, dt("2026-07-03T00:00:00Z")),
        ]


class FakeFilingProvider(FilingProvider):
    def get_filings(self, instrument: Instrument, limit: int = 10) -> list[Filing]:
        return [
            Filing(
                instrument,
                "10-Q",
                dt("2026-06-30T00:00:00Z"),
                dt("2026-07-02T18:00:00Z"),
                dt("2026-06-30T00:00:00Z"),
                "https://sec.example/filing",
                "SEC EDGAR submissions",
                dt("2026-07-06T00:00:00Z"),
                "abc",
                "0000000000-26-000001",
            )
        ]

    def get_company_facts(self, instrument: Instrument) -> list[FinancialFact]:
        return [
            FinancialFact(
                instrument,
                "Revenues",
                Decimal("1000"),
                "USD",
                dt("2026-04-01T00:00:00Z"),
                dt("2026-06-30T00:00:00Z"),
                dt("2026-07-02T00:00:00Z"),
                None,
                "SEC EDGAR companyfacts",
                "https://sec.example/facts",
                dt("2026-07-06T00:00:00Z"),
                "10-Q",
            ),
            FinancialFact(
                instrument,
                "NetIncomeLoss",
                Decimal("120"),
                "USD",
                dt("2026-04-01T00:00:00Z"),
                dt("2026-06-30T00:00:00Z"),
                dt("2026-07-02T00:00:00Z"),
                None,
                "SEC EDGAR companyfacts",
                "https://sec.example/facts",
                dt("2026-07-06T00:00:00Z"),
                "10-Q",
            ),
        ]


class FakeNewsProvider(NewsProvider):
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def search_news(self, instrument: Instrument, start: datetime, end: datetime, limit: int = 20) -> list[NewsItem]:
        if self.fail:
            raise ProviderTimeoutError("news timeout")
        return [
            NewsItem("NVIDIA announces product update", dt("2026-07-03T14:00:00Z"), dt("2026-07-06T00:00:00Z"), "Test News", "https://news.example/1", "Product update", "en", ("NVDA",)),
            NewsItem("Analyst discusses NVIDIA demand", dt("2026-07-03T15:00:00Z"), dt("2026-07-06T00:00:00Z"), "Test News", "https://news.example/2", "Demand commentary", "en", ("NVDA",)),
            NewsItem("NVIDIA supplier commentary", dt("2026-07-03T16:00:00Z"), dt("2026-07-06T00:00:00Z"), "Test News", "https://news.example/3", "Supply commentary", "en", ("NVDA",)),
        ]


class FakeHttpClient:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.last_headers = None

    def get(self, url: str, *, headers=None, cache_key=None) -> HttpResponse:
        self.last_headers = headers
        return HttpResponse(url, 200, {}, self.body, 1, 0)


class VerticalSliceTests(unittest.TestCase):
    def test_point_in_time_guard_filters_future_evidence(self) -> None:
        past = Evidence("E1", EvidenceType.NEWS, "test", None, dt("2026-01-01T00:00:00Z"), dt("2026-01-01T00:00:00Z"), dt("2026-01-01T00:00:00Z"), dt("2026-01-02T00:00:00Z"), "past")
        future = Evidence("E2", EvidenceType.NEWS, "test", None, dt("2026-01-03T00:00:00Z"), dt("2026-01-03T00:00:00Z"), dt("2026-01-03T00:00:00Z"), dt("2026-01-04T00:00:00Z"), "future")
        filtered = filter_evidence_as_of([past, future], AsOfContext(dt("2026-01-02T00:00:00Z")))
        self.assertEqual([item.evidence_id for item in filtered], ["E1"])
        with self.assertRaises(PointInTimeViolation):
            assert_available_as_of(future, AsOfContext(dt("2026-01-02T00:00:00Z")))

    def test_evidence_normalization_and_deduplication(self) -> None:
        fact = FakeFilingProvider().get_company_facts(INSTRUMENT)[0]
        ev = financial_fact_to_evidence(fact)
        self.assertEqual(ev.evidence_type, EvidenceType.FINANCIAL_FACT)
        self.assertIn("Revenues", ev.content)
        self.assertEqual(len(dedupe_evidence([ev, ev])), 1)

    def test_timeline_ordering(self) -> None:
        evidence = normalize_all(
            bars=FakeMarketProvider().get_bars(INSTRUMENT, dt("2026-07-01T00:00:00Z"), dt("2026-07-06T00:00:00Z")),
            filings=FakeFilingProvider().get_filings(INSTRUMENT),
            facts=FakeFilingProvider().get_company_facts(INSTRUMENT),
            news=FakeNewsProvider().search_news(INSTRUMENT, dt("2026-07-01T00:00:00Z"), dt("2026-07-06T00:00:00Z")),
        )
        timeline = build_timeline(evidence)
        self.assertEqual(list(timeline), sorted(timeline, key=lambda item: (item.timestamp, item.evidence_id)))

    def test_abnormal_return_and_volume_detection(self) -> None:
        evidence = normalize_all(bars=FakeMarketProvider().get_bars(INSTRUMENT, dt("2026-07-01T00:00:00Z"), dt("2026-07-06T00:00:00Z")))
        events = detect_events(INSTRUMENT, evidence, EventDetectionConfig(abnormal_return_abs=Decimal("0.08"), abnormal_volume_ratio=Decimal("3")))
        event_types = {event.event_type.value for event in events}
        self.assertIn("abnormal_return", event_types)
        self.assertIn("abnormal_volume", event_types)

    def test_marketdata_app_provider_parses_daily_bars(self) -> None:
        provider = MarketDataAppPrototypeMarketDataProvider(
            http=FakeHttpClient(
                b'{"s":"ok","t":[1782864000],"o":[100.1],"h":[101.2],"l":[99.8],"c":[100.9],"v":[12345]}'
            )
        )
        bars = provider.get_bars(INSTRUMENT, dt("2026-07-01T00:00:00Z"), dt("2026-07-06T00:00:00Z"))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].source, "MarketData.app prototype candles")
        self.assertEqual(bars[0].close, Decimal("100.9"))

    def test_marketdata_app_provider_rejects_malformed_ohlcv(self) -> None:
        provider = MarketDataAppPrototypeMarketDataProvider(http=FakeHttpClient(b'{"s":"ok","t":[1782864000],"o":[100.1]}'))
        with self.assertRaises(MalformedResponseError):
            provider.get_bars(INSTRUMENT, dt("2026-07-01T00:00:00Z"), dt("2026-07-06T00:00:00Z"))

    def test_marketdata_app_provider_uses_bearer_token_when_configured(self) -> None:
        http = FakeHttpClient(b'{"s":"ok","t":[1782864000],"o":[100.1],"h":[101.2],"l":[99.8],"c":[100.9],"v":[12345]}')
        provider = MarketDataAppPrototypeMarketDataProvider(http=http, token="test-token")
        provider.get_bars(INSTRUMENT, dt("2026-07-01T00:00:00Z"), dt("2026-07-06T00:00:00Z"))
        self.assertEqual(http.last_headers["Authorization"], "Bearer test-token")

    def test_confidence_calculation(self) -> None:
        evidence = normalize_all(filings=FakeFilingProvider().get_filings(INSTRUMENT), facts=FakeFilingProvider().get_company_facts(INSTRUMENT))
        score, reasons = calculate_confidence(evidence, [], dt("2026-07-06T00:00:00Z"))
        self.assertGreater(score, 0.5)
        self.assertTrue(reasons)

    def test_integration_provider_to_report_with_citations(self) -> None:
        pipeline = InvestigationPipeline(FakeMarketProvider(), FakeFilingProvider(), FakeNewsProvider())
        result = pipeline.investigate("NVDA", as_of=AsOfContext(dt("2026-07-06T00:00:00Z")), lookback_days=10)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.investigation_id, result.report.investigation_id)
        self.assertGreater(len(result.evidence), 0)
        self.assertGreater(len(result.report.evidence_refs), 0)
        evidence_ids = {item.evidence_id for item in result.evidence}
        self.assertTrue(set(result.report.evidence_refs).issubset(evidence_ids))
        self.assertIn("[", result.report.summary)

    def test_pipeline_reuse_does_not_cross_contaminate_symbols(self) -> None:
        class MultiMarketProvider(FakeMarketProvider):
            def resolve_instrument(self, symbol: str) -> Instrument:
                return Instrument(symbol.upper(), "NASDAQ", Market.US, "USD", f"{symbol.upper()} Corp", cik="0000000000")

        pipeline = InvestigationPipeline(MultiMarketProvider(), FakeFilingProvider(), FakeNewsProvider())
        first = pipeline.investigate("NVDA", as_of=AsOfContext(dt("2026-07-06T00:00:00Z")), lookback_days=10)
        second = pipeline.investigate("AAPL", as_of=AsOfContext(dt("2026-07-06T00:00:00Z")), lookback_days=10)
        self.assertTrue(first.evidence)
        self.assertTrue(second.evidence)
        self.assertTrue(all(item.metadata.get("symbol") == "AAPL" for item in second.evidence if item.metadata.get("symbol")))
        self.assertFalse({item.evidence_id for item in first.evidence} & {item.evidence_id for item in second.evidence})

    def test_provider_failure_becomes_unknown_not_silent(self) -> None:
        pipeline = InvestigationPipeline(FakeMarketProvider(fail=True), FakeFilingProvider(), FakeNewsProvider(fail=True))
        result = pipeline.investigate("NVDA", as_of=AsOfContext(dt("2026-07-06T00:00:00Z")), lookback_days=10)
        self.assertTrue(result.provider_warnings)
        self.assertTrue(any("Market data provider failed" in item for item in result.report.unknowns))
        self.assertTrue(any("News provider failed" in item for item in result.report.unknowns))

    def test_malformed_json_failure(self) -> None:
        response = HttpResponse("https://example.test", 200, {}, b"{bad", 1, 0)
        with self.assertRaises(MalformedResponseError):
            response.json()

    def test_lookahead_regression(self) -> None:
        prices = [100, 101, 99, 103, 102, 106]

        def run(signal):
            cash = 1.0
            pos = 0.0
            for i in range(1, len(prices)):
                if signal(i) > 0:
                    pos = cash / prices[i - 1]
                    cash = 0.0
                else:
                    cash = pos * prices[i - 1] if pos else cash
                    pos = 0.0
            return cash + pos * prices[-1] - 1.0

        valid_lagged = lambda i: prices[i - 1] - prices[i - 2] if i >= 2 else 0
        invalid_future = lambda i: prices[i] - prices[i - 1]
        self.assertLess(run(valid_lagged), run(invalid_future))
        feature_time = dt("2026-01-03T00:00:00Z")
        decision_time = dt("2026-01-02T00:00:00Z")
        self.assertGreater(feature_time, decision_time)


if __name__ == "__main__":
    unittest.main()
