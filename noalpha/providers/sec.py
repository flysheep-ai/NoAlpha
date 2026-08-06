from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any

from noalpha.domain import FinancialFact, Filing, Instrument, parse_datetime, now_utc, stable_id
from noalpha.errors import MalformedResponseError, NotFoundError
from noalpha.http import HttpClient
from noalpha.provider_health import ProviderHealth, ProviderHealthStatus
from noalpha.providers.base import FilingProvider


logger = logging.getLogger(__name__)

SEC_DATA_BASE = "https://data.sec.gov"
SEC_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"


class SecFilingProvider(FilingProvider):
    def __init__(
        self,
        *,
        user_agent: str = "NoAlpha research contact@example.com",
        cache_dir: Path | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.http = http or HttpClient(
            user_agent=user_agent,
            timeout=20,
            max_retries=2,
            retry_backoff=1.0,
            cache_dir=cache_dir,
            min_interval_sec=0.13,
        )

    def get_filings(self, instrument: Instrument, limit: int = 10) -> list[Filing]:
        cik = self._require_cik(instrument)
        data = self._get_submissions(cik)
        recent = data.get("filings", {}).get("recent", {})
        required = ["accessionNumber", "form", "filingDate", "acceptanceDateTime", "reportDate", "primaryDocument"]
        if not all(key in recent for key in required):
            raise MalformedResponseError(f"SEC submissions missing required fields for {instrument.symbol}")

        filings: list[Filing] = []
        rows = zip(*(recent[key] for key in required), strict=False)
        for accession, form, filing_date, accepted, report_date, primary_doc in rows:
            if len(filings) >= limit:
                break
            accepted_at = parse_datetime(accepted)
            filed_at = parse_datetime(filing_date)
            period_end = parse_datetime(report_date) if report_date else None
            if not accepted_at or not filed_at:
                continue
            accession_no_dash = str(accession).replace("-", "")
            url = f"{SEC_ARCHIVE_BASE}/{int(cik)}/{accession_no_dash}/{primary_doc}"
            content_hash = sha256(f"{accession}|{form}|{accepted}|{primary_doc}".encode("utf-8")).hexdigest()
            filings.append(
                Filing(
                    instrument=instrument,
                    filing_type=str(form),
                    filing_date=filed_at,
                    accepted_at=accepted_at,
                    period_end=period_end,
                    source_url=url,
                    source="SEC EDGAR submissions",
                    retrieved_at=now_utc(),
                    content_hash=content_hash,
                    accession_number=str(accession),
                )
            )
        return filings

    def get_company_facts(self, instrument: Instrument) -> list[FinancialFact]:
        cik = self._require_cik(instrument)
        data = self._get_companyfacts(cik)
        facts = data.get("facts", {}).get("us-gaap", {})
        if not isinstance(facts, dict):
            raise MalformedResponseError(f"SEC companyfacts malformed for {instrument.symbol}")

        selected_concepts = (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "NetIncomeLoss",
            "Assets",
            "Liabilities",
            "StockholdersEquity",
            "EarningsPerShareDiluted",
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "OperatingIncomeLoss",
            "CashAndCashEquivalentsAtCarryingValue",
        )
        results: list[FinancialFact] = []
        for concept in selected_concepts:
            concept_data = facts.get(concept)
            if not isinstance(concept_data, dict):
                continue
            units = concept_data.get("units", {})
            if not isinstance(units, dict):
                continue
            for unit, values in units.items():
                if not isinstance(values, list):
                    continue
                for item in values:
                    fact = self._parse_fact_item(instrument, cik, concept, unit, item)
                    if fact:
                        results.append(fact)

        results.sort(key=lambda f: (f.period_end, f.filed_at, f.concept))
        return results

    def _parse_fact_item(
        self,
        instrument: Instrument,
        cik: str,
        concept: str,
        unit: str,
        item: dict[str, Any],
    ) -> FinancialFact | None:
        try:
            value = Decimal(str(item["val"]))
        except (KeyError, InvalidOperation):
            return None
        period_end = parse_datetime(item.get("end"))
        filed_at = parse_datetime(item.get("filed"))
        if not period_end or not filed_at:
            return None
        period_start = parse_datetime(item.get("start"))
        accession = item.get("accn")
        source_url = SEC_DATA_BASE + f"/api/xbrl/companyfacts/CIK{int(cik):010}.json"
        return FinancialFact(
            instrument=instrument,
            concept=concept,
            value=value,
            unit=str(unit),
            period_start=period_start,
            period_end=period_end,
            filed_at=filed_at,
            accepted_at=None,
            source="SEC EDGAR companyfacts",
            source_url=source_url + (f"#accn={accession}" if accession else ""),
            retrieved_at=now_utc(),
            form=item.get("form"),
            taxonomy="us-gaap",
            fiscal_year=int(item["fy"]) if item.get("fy") is not None else None,
            fiscal_period=item.get("fp"),
            accession_number=str(accession) if accession else None,
            frame=item.get("frame"),
            dimensions={},
        )

    def _get_submissions(self, cik: str) -> dict[str, Any]:
        padded = f"{int(cik):010}"
        response = self.http.get(
            f"{SEC_DATA_BASE}/submissions/CIK{padded}.json",
            headers={"Accept": "application/json"},
            cache_key=f"sec_submissions_{padded}.json",
        )
        data = response.json()
        if not isinstance(data, dict):
            raise MalformedResponseError(f"SEC submissions was not an object for CIK {cik}")
        return data

    def _get_companyfacts(self, cik: str) -> dict[str, Any]:
        padded = f"{int(cik):010}"
        response = self.http.get(
            f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{padded}.json",
            headers={"Accept": "application/json"},
            cache_key=f"sec_companyfacts_{padded}.json",
        )
        data = response.json()
        if not isinstance(data, dict):
            raise MalformedResponseError(f"SEC companyfacts was not an object for CIK {cik}")
        return data

    def _require_cik(self, instrument: Instrument) -> str:
        if not instrument.cik:
            raise NotFoundError(f"Instrument {instrument.symbol} has no CIK")
        return instrument.cik

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider="SEC EDGAR",
            status=ProviderHealthStatus.HEALTHY,
            requires_credentials=False,
            supports_historical=True,
            supports_realtime=False,
            supports_intraday=False,
            coverage="US SEC registrants with CIK metadata",
        )


def financial_fact_id(fact: FinancialFact) -> str:
    return stable_id(
        "financial_fact",
        fact.instrument.symbol,
        fact.concept,
        fact.unit,
        fact.period_end.isoformat(),
        fact.filed_at.isoformat(),
        fact.value,
    )
