from __future__ import annotations

import os
import unittest
from pathlib import Path

from noalpha.domain import AsOfContext
from noalpha.domain import EvidenceType
from noalpha.pipeline import InvestigationPipeline


@unittest.skipUnless(os.environ.get("RUN_ONLINE_SMOKE") == "1", "online smoke disabled")
class OnlineSmokeTests(unittest.TestCase):
    def test_aapl_online_smoke(self) -> None:
        pipeline = InvestigationPipeline.default(cache_dir=Path(".cache/test_online"))
        result = pipeline.investigate("AAPL", as_of=AsOfContext.now(), lookback_days=45, filings_limit=3, news_limit=5)
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.evidence)
        self.assertTrue(result.report.evidence_refs)
        self.assertIn(EvidenceType.MARKET_BAR, {item.evidence_type for item in result.evidence})


if __name__ == "__main__":
    unittest.main()
