from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from noalpha.api import create_app


class FakeInvestigationResult:
    investigation_id = "INV-1"

    def to_dict(self):
        return {
            "investigation_id": "INV-1",
            "status": "completed",
            "provider_warnings": ["Market data provider failed: HTTP 401"],
            "quality_report": {
                "raw_evidence_count": 2,
                "canonical_evidence_count": 2,
                "duplicate_count": 0,
                "provider_health": {"market_data": {"status": "unauthorized", "provider": "MarketData.app"}},
                "citation_support": {"citation_support_rate": 1.0, "supported_claims": 1, "unsupported_claims": 0},
            },
            "report": {
                "investigation_id": "INV-1",
                "subject": {"symbol": "AAPL", "company_name": "Apple Inc.", "exchange": "NASDAQ", "market": "US", "currency": "USD", "sector": None, "industry": None, "cik": "0000320193"},
                "generated_at": "2026-07-06T00:00:00Z",
                "as_of": "2026-07-06T00:00:00Z",
                "summary": "AAPL report [E1].",
                "bull_case": ["Bull [E1]."],
                "bear_case": [],
                "risks": [],
                "unknowns": ["Market data unavailable"],
                "evidence_refs": ["E1"],
                "confidence": 0.61,
                "confidence_reasons": ["test"],
                "confidence_breakdown": {"overall": 0.61, "market_data": 0.0, "fundamentals": 0.9, "filings": 0.9, "news": 0.7, "temporal_integrity": 1.0, "citation_support": 1.0},
                "claims": [{"claim_id": "C1", "text": "AAPL has evidence.", "claim_type": "observation", "evidence_ids": ["E1"], "inference_level": "direct", "confidence": 0.9}],
                "timeline": [{"timestamp": "2026-07-06T00:00:00Z", "evidence_id": "E1", "title": "Evidence", "source": "Test", "source_url": None, "metadata": {}}],
                "detected_events": [],
                "quality_report": {},
            },
            "evidence": [
                {
                    "evidence_id": "E1",
                    "evidence_type": "news",
                    "source": "Test News",
                    "source_url": "https://example.test",
                    "event_time": "2026-07-06T00:00:00Z",
                    "published_at": "2026-07-06T00:00:00Z",
                    "available_at": "2026-07-06T00:00:00Z",
                    "retrieved_at": "2026-07-06T00:00:00Z",
                    "content": "Evidence",
                    "metadata": {"importance_score": 0.8, "availability_uncertain": False},
                }
            ],
        }


class FakePipeline:
    def investigate(self, symbol, *, as_of=None, lookback_days=60, filings_limit=8, news_limit=20,
                    progress=None, report_language="en-US", **kwargs):
        return FakeInvestigationResult()


class ApiTests(unittest.TestCase):
    def test_admin_token_required_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"NOALPHA_ADMIN_TOKEN": "secret"}):
                client = TestClient(create_app(Path(tmp)))
                denied = client.put(
                    "/api/v1/settings/credentials",
                    json={"key": "LLM_MODEL", "value": "model-a"},
                )
                self.assertEqual(denied.status_code, 401)
                allowed = client.put(
                    "/api/v1/settings/credentials",
                    headers={"Authorization": "Bearer secret"},
                    json={"key": "LLM_MODEL", "value": "model-a"},
                )
                self.assertEqual(allowed.status_code, 200)

    def test_create_and_get_investigation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Path(tmp))
            app.state.pipeline_factory = lambda **kwargs: FakePipeline()
            client = TestClient(app)
            created = client.post("/api/v1/investigations", json={"symbol": "AAPL", "lookback_days": 60})
            self.assertEqual(created.status_code, 202)
            job_id = created.json()["investigation_id"]
            job = client.get(f"/api/v1/investigations/{job_id}")
            self.assertEqual(job.status_code, 200)
            body = job.json()
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["report"]["confidence"], 0.61)
            self.assertEqual(body["report"]["investigation_id"], job_id)

    def test_evidence_filter_and_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Path(tmp))
            app.state.pipeline_factory = lambda **kwargs: FakePipeline()
            client = TestClient(app)
            created = client.post("/api/v1/investigations", json={"symbol": "AAPL"})
            response = client.get(f"/api/v1/investigations/{created.json()['investigation_id']}/evidence", params={"evidence_type": "news", "limit": 1})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 1)

    def test_provider_health_serializes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(Path(tmp)))
            response = client.get("/api/v1/providers/health")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["items"])

    def test_failed_investigation_is_explicit(self) -> None:
        class FailingPipeline:
            def investigate(self, *args, **kwargs):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Path(tmp))
            app.state.pipeline_factory = lambda **kwargs: FailingPipeline()
            client = TestClient(app)
            created = client.post("/api/v1/investigations", json={"symbol": "AAPL"})
            self.assertEqual(created.status_code, 202)
            pending_id = created.json()["investigation_id"]
            response = client.get(f"/api/v1/investigations/{pending_id}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "failed")
            self.assertIn("boom", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
