from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.metrics.research_quality import evaluate_replay_case
from noalpha.domain import AsOfContext, parse_datetime
from noalpha.pipeline import InvestigationPipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Run historical replay quality cases.")
    parser.add_argument("--cases", default="evals/datasets/historical_cases.json")
    parser.add_argument("--cache-dir", default=".cache/evals")
    parser.add_argument("--out", default="evals/reports/replay_results.json")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text())
    pipeline = InvestigationPipeline.default(cache_dir=Path(args.cache_dir))
    results = []
    for case in cases:
        as_of = parse_datetime(case["as_of"])
        if as_of is None:
            raise ValueError(f"Invalid as_of for {case['case_id']}")
        result = pipeline.investigate(
            case["symbol"],
            as_of=AsOfContext(as_of),
            lookback_days=int(case.get("lookback_days", 60)),
        )
        result_dict = result.to_dict()
        results.append({"case": case, "metrics": evaluate_replay_case(result_dict, case), "result": result_dict})

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(json.dumps({"cases": len(results), "out": str(out_path)}, indent=2))


if __name__ == "__main__":
    main()
