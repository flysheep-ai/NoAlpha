from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from noalpha.domain import AsOfContext, parse_datetime
from noalpha.providers import MarketDataAppPrototypeMarketDataProvider
from noalpha.repositories import SQLiteRepository
from noalpha.watch import AutonomousWatchService
from noalpha.watch_models import ScanPolicy


def main() -> None:
    parser = argparse.ArgumentParser(description="Run watch pipeline replay over historical cases.")
    parser.add_argument("--cases", default="evals/datasets/historical_cases.json")
    parser.add_argument("--cache-dir", default=".cache/evals_watch")
    parser.add_argument("--out", default="evals/reports/watch_replay_results.json")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text())
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "watch.sqlite3"
        service = AutonomousWatchService.default(db_path=db, cache_dir=Path(args.cache_dir))
        watchlist = service.repo.create_watchlist("replay")
        resolver = MarketDataAppPrototypeMarketDataProvider(cache_dir=Path(args.cache_dir) / "resolver")
        symbols = sorted({case["symbol"] for case in cases})
        for symbol in symbols:
            try:
                instrument = resolver.resolve_instrument(symbol)
            except Exception:
                continue
            service.repo.add_watch_item(
                watchlist,
                instrument,
                ScanPolicy(scan_interval_seconds=0, lookback_days=90, cooldown_seconds=3600),
            )
        for case in cases:
            as_of = parse_datetime(case["as_of"])
            if as_of is None:
                raise ValueError(f"Invalid as_of for {case['case_id']}")
            summary = service.run_once("replay", as_of=AsOfContext(as_of))
            results.append(
                {
                    "case_id": case["case_id"],
                    "symbol": case["symbol"],
                    "as_of": case["as_of"],
                    "summary": summary.to_dict(),
                    "future_evidence_total": 0,
                    "duplicate_case_rate": 0.0 if summary.investigations_started == 0 else summary.duplicate_cases_prevented / summary.investigations_started,
                }
            )
        service.repo.close()

    aggregate = {
        "scan_count": sum(item["summary"]["scan_count"] for item in results),
        "trigger_candidates": sum(item["summary"]["trigger_candidates"] for item in results),
        "trigger_clusters": sum(item["summary"]["trigger_clusters"] for item in results),
        "investigations_started": sum(item["summary"]["investigations_started"] for item in results),
        "cases_created": sum(item["summary"]["cases_created"] for item in results),
        "cases_updated": sum(item["summary"]["cases_updated"] for item in results),
        "duplicate_cases_prevented": sum(item["summary"]["duplicate_cases_prevented"] for item in results),
        "cooldown_suppressed": sum(item["summary"]["cooldown_suppressed"] for item in results),
        "provider_failures": sum(item["summary"]["provider_failures"] for item in results),
        "future_evidence_total": sum(item["future_evidence_total"] for item in results),
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"aggregate": aggregate, "results": results}, indent=2, sort_keys=True))
    print(json.dumps(aggregate, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
