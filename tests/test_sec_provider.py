from __future__ import annotations

import json
import unittest

from noalpha.domain import Instrument, Market
from noalpha.http import HttpResponse
from noalpha.providers.sec import SecFilingProvider


class FakeHttp:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, *, headers=None, cache_key=None):
        self.calls.append((url, cache_key))
        if "submissions" in url:
            return HttpResponse(
                url,
                200,
                {},
                json.dumps(
                    {
                        "filings": {
                            "recent": {
                                "accessionNumber": ["0001045810-26-000001"],
                                "form": ["10-Q"],
                                "filingDate": ["2026-07-02"],
                                "acceptanceDateTime": ["2026-07-02T18:00:00.000Z"],
                                "reportDate": ["2026-06-30"],
                                "primaryDocument": ["nvda-20260630.htm"],
                            }
                        }
                    }
                ).encode(),
                1,
                0,
            )
        return HttpResponse(
            url,
            200,
            {},
            json.dumps(
                {
                    "facts": {
                        "us-gaap": {
                            "Revenues": {
                                "units": {
                                    "USD": [
                                        {
                                            "val": 100,
                                            "start": "2026-04-01",
                                            "end": "2026-06-30",
                                            "filed": "2026-07-02",
                                            "form": "10-Q",
                                            "accn": "0001045810-26-000001",
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            ).encode(),
            1,
            0,
        )


class SecProviderTests(unittest.TestCase):
    def test_sec_provider_parses_filings_and_facts(self) -> None:
        instrument = Instrument("NVDA", "NASDAQ", Market.US, "USD", "NVIDIA Corporation", cik="0001045810")
        provider = SecFilingProvider(http=FakeHttp())
        filings = provider.get_filings(instrument)
        facts = provider.get_company_facts(instrument)
        self.assertEqual(filings[0].filing_type, "10-Q")
        self.assertEqual(filings[0].accepted_at.year, 2026)
        self.assertEqual(facts[0].concept, "Revenues")
        self.assertEqual(str(facts[0].value), "100")


if __name__ == "__main__":
    unittest.main()
