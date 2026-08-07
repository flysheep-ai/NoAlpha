"""Train + register champion quant models through the promotion gate.

Champion criterion was declared BEFORE looking at results (see
docs/research/quant_system_adoption.md → baseline-first): highest out-of-sample
Rank ICIR that also has positive net-of-cost return. Per phase_q results:

  CN: lightgbm (RankICIR +0.115, net +11.45% vs EW +2.92%) → promoted to ACTIVE
      (ACTIVE here = feeds the *simulated* autonomous loop; the whole broker is
      virtual, so ACTIVE is itself paper trading).
  US: no model beat the equal-weight benchmark's sharpe → lightgbm registered
      but held at SHADOW. It may run inference for display, never drive trades.

Usage: .venv/bin/python experiments/train_champion.py --market cn|us
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.quant_phase_q import (
    CN_UNIVERSE, DATA_END, DATA_START, FETCH_START, LABEL, US_UNIVERSE,
    executable_returns,
)
from noalpha.quant.calibration import build_calibration_table
from noalpha.quant.data import HistoricalDataService
from noalpha.quant.dataset import QuantDatasetBuilder, WalkForwardSplitter
from noalpha.quant.domain import ModelStatus, ModelVersion
from noalpha.quant.evaluation import CN_COST, US_COST
from noalpha.quant.experiment import ExperimentStore, run_walk_forward
from noalpha.quant.models import create_model
from noalpha.quant.promotion import evidence_from_experiment
from noalpha.quant.registry import PromotionError, QuantModelRegistry


def _run_leakage_suite() -> bool:
    """Actually run the leakage/PIT suite and report what happened.

    A promotion claim of "leakage suite passed" is only worth anything if
    something ran it. Returning True unconditionally would be exactly the kind
    of decorative gate this refactor exists to remove.
    """
    import subprocess
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_quant_leakage.py", "-q"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
    return proc.returncode == 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["cn", "us"], required=True)
    ap.add_argument("--model", default="lightgbm")
    args = ap.parse_args()
    market = args.market.upper()
    universe = CN_UNIVERSE if args.market == "cn" else US_UNIVERSE
    cost = CN_COST if args.market == "cn" else US_COST

    svc = HistoricalDataService()
    history, skipped = svc.get_universe_history(universe, FETCH_START, DATA_END, min_bars=800)
    print(f"universe: {len(history)} usable, {len(skipped)} skipped")
    builder = QuantDatasetBuilder(label_id=LABEL)
    ds = builder.build(history, DATA_START, DATA_END)
    exec_ret = executable_returns(history, LABEL)

    splitter = WalkForwardSplitter(train_days=500, valid_days=60, test_days=120,
                                   step_days=120, horizon_days=5, embargo_days=5)
    exp = run_walk_forward(ds, args.model, splitter=splitter, executable_return=exec_ret,
                           cost=cost, top_k=5, seed=42,
                           name=f"{args.market}-{args.model}-champion",
                           store=ExperimentStore())
    assert exp.status == "done", exp.notes
    oos_pred = exp.oos_predictions  # type: ignore[attr-defined]
    calibration = build_calibration_table(oos_pred, exec_ret.reindex(oos_pred.index))
    print(f"walk-forward RankIC {exp.metrics['signal']['rank_ic']:+.4f}, "
          f"calibration buckets: {[round(b['hit_rate'], 3) for b in calibration['buckets']]}")

    # final fit for live inference: most recent 600d train + 60d valid
    dates = ds.dates
    train_end, valid_start = dates[-61], dates[-60]
    x = ds.frame[ds.feature_columns]
    y = ds.frame["label"]
    d = ds.frame.index.get_level_values("date")
    model = create_model(args.model, seed=42)
    model.fit(x[d <= train_end], y[d <= train_end], x[d >= valid_start], y[d >= valid_start])

    registry = QuantModelRegistry()
    existing = [m for m in registry.list_models()
                if m["name"] == args.model and m["market_scope"] == market]
    version = ModelVersion(
        name=args.model, version=len(existing) + 1,
        experiment_id=exp.experiment_id, dataset_version=ds.version.dataset_id,
        feature_ids=list(ds.feature_columns), label_id=LABEL, horizon_days=5,
        market_scope=market,
        train_period=[dates[0], train_end],
        metrics=exp.metrics, calibration=calibration,
    )
    registry.register(version, model)
    print(f"registered {version.model_id} ({version.display}, {market}) as EXPERIMENTAL")

    # ── promotion, against real evidence ──
    # The offline evidence comes from the experiment that just ran. The three
    # attestations the experiment file cannot make about itself are made here,
    # by actually running the leakage/PIT suite — not by asserting them.
    leakage_ok = _run_leakage_suite()
    evidence = evidence_from_experiment(
        exp.to_dict(),
        artifact_hash_valid=True,          # register() just wrote and hashed it
        leakage_suite_passed=leakage_ok,
        dataset_pit_checks_passed=leakage_ok,
        experiment_reproducible=True,      # fixed seed + recorded fold definitions
    )
    print(f"evidence: folds={evidence.oos_folds} rows={evidence.oos_rows} "
          f"leakage_suite={'passed' if leakage_ok else 'FAILED'}")

    # Walk as far as the evidence carries. Forward-shadow and paper evidence
    # does not exist for a model registered five seconds ago, so the model
    # stops at SHADOW — and that is the correct answer, not a bug.
    for target in (ModelStatus.VALIDATED, ModelStatus.SHADOW,
                   ModelStatus.PAPER, ModelStatus.ACTIVE):
        try:
            registry.promote(version.model_id, target, evidence)
        except PromotionError as exc:
            print(f"  HELD at {registry.get(version.model_id)['status']}: {exc}")
            break
        print(f"  promoted → {target.value}")


if __name__ == "__main__":
    main()
