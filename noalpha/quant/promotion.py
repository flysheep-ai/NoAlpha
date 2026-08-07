"""Evidence-based model promotion.

The old gate answered exactly one question — "is this the next stage?" — and
nothing else. A model could walk EXPERIMENTAL → VALIDATED → SHADOW → PAPER →
ACTIVE in five API calls with no out-of-sample evidence, no leakage suite, no
benchmark, no forward observation. The lifecycle looked rigorous and enforced
nothing.

A promotion here is a claim about evidence, so it takes evidence:

    PromotionEvidenceBundle   what is actually known about the model
    PromotionPolicy           what each stage requires
    PromotionDecision         approved/denied + every unmet requirement

Design rules this module holds itself to:

  * requirements live in ONE table, not scattered if/else
  * every requirement names the field that satisfies it, so a denial says
    exactly what is missing rather than "not eligible"
  * thresholds are stage properties, never tuned per model — a model that
    does not clear them STAYS IN SHADOW, which is the point of NoAlpha
  * evidence absent ≠ evidence passed. A missing field fails.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from noalpha.quant.domain import ModelStatus


@dataclass
class PromotionEvidenceBundle:
    """Everything a promotion decision is allowed to consider.

    Every field defaults to the pessimistic value. Nothing is assumed to have
    passed just because it was not measured.
    """
    # ── EXPERIMENTAL → VALIDATED: is the experiment even trustworthy? ──
    dataset_pit_checks_passed: bool = False
    leakage_suite_passed: bool = False
    oos_folds: int = 0
    oos_rows: int = 0
    experiment_reproducible: bool = False
    artifact_hash_valid: bool = False

    # ── VALIDATED → SHADOW: does it beat anything, after costs? ──
    oos_metrics: dict[str, Any] = field(default_factory=dict)
    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    cost_adjusted_backtest: bool = False
    critical_data_quality_warnings: list[str] = field(default_factory=list)

    # ── SHADOW → PAPER: does it hold up going forward? ──
    forward_shadow_cycles: int = 0
    forward_decisions: int = 0
    market_regimes_observed: int = 0
    stability_checks_passed: bool = False
    severe_drift_detected: bool = True

    # ── PAPER → ACTIVE: can it actually trade? ──
    paper_decisions: int = 0
    risk_limit_breaches: int = -1          # -1 = never measured
    execution_success_rate: float = -1.0   # -1 = never measured
    paper_return: float | None = None
    benchmark_return: float | None = None

    # ── provenance, always recorded ──
    experiment_id: str = ""
    dataset_id: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Requirement:
    name: str
    describe: str
    check: Callable[[PromotionEvidenceBundle], bool]

    def evaluate(self, evidence: PromotionEvidenceBundle) -> dict[str, Any]:
        try:
            passed = bool(self.check(evidence))
        except Exception as exc:                      # a broken check is a failure
            return {"name": self.name, "passed": False, "requirement": self.describe,
                    "detail": f"check raised {type(exc).__name__}: {exc}"}
        return {"name": self.name, "passed": passed, "requirement": self.describe}


@dataclass
class PromotionDecision:
    model_id: str
    current: str
    target: str
    approved: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    override: bool = False
    override_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── thresholds ──
# Deliberately stated once, here, so nobody can argue a model through by
# editing a condition at the call site.

MIN_OOS_FOLDS = 3
MIN_OOS_ROWS = 500
MIN_FORWARD_SHADOW_CYCLES = 20
MIN_FORWARD_DECISIONS = 30
MIN_MARKET_REGIMES = 2
MIN_PAPER_DECISIONS = 30
MIN_EXECUTION_SUCCESS_RATE = 0.95


def _has_positive(metrics: dict[str, Any], *path: str) -> float | None:
    node: Any = metrics
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    try:
        return float(node)
    except (TypeError, ValueError):
        return None


REQUIREMENTS: dict[tuple[ModelStatus, ModelStatus], list[Requirement]] = {
    (ModelStatus.EXPERIMENTAL, ModelStatus.VALIDATED): [
        Requirement("dataset_pit_checks", "dataset point-in-time checks passed",
                    lambda e: e.dataset_pit_checks_passed),
        Requirement("leakage_suite", "leakage attack suite passed",
                    lambda e: e.leakage_suite_passed),
        Requirement("oos_folds", f"at least {MIN_OOS_FOLDS} out-of-sample folds",
                    lambda e: e.oos_folds >= MIN_OOS_FOLDS),
        Requirement("oos_rows", f"at least {MIN_OOS_ROWS} out-of-sample rows",
                    lambda e: e.oos_rows >= MIN_OOS_ROWS),
        Requirement("reproducible", "experiment reproduces from recorded provenance",
                    lambda e: e.experiment_reproducible),
        Requirement("artifact_hash", "model artifact hash verifies",
                    lambda e: e.artifact_hash_valid),
    ],
    (ModelStatus.VALIDATED, ModelStatus.SHADOW): [
        Requirement("oos_metrics", "out-of-sample metrics recorded",
                    lambda e: bool(e.oos_metrics)),
        Requirement("baseline_comparison", "a baseline was measured on the same window",
                    lambda e: bool(e.baseline_metrics)),
        Requirement("cost_adjusted_backtest", "backtest is net of the cost model",
                    lambda e: e.cost_adjusted_backtest),
        Requirement("no_critical_data_quality_warning",
                    "no critical data-quality warning outstanding",
                    lambda e: not e.critical_data_quality_warnings),
    ],
    (ModelStatus.SHADOW, ModelStatus.PAPER): [
        Requirement("forward_shadow_duration",
                    f"at least {MIN_FORWARD_SHADOW_CYCLES} forward shadow cycles",
                    lambda e: e.forward_shadow_cycles >= MIN_FORWARD_SHADOW_CYCLES),
        Requirement("forward_decisions",
                    f"at least {MIN_FORWARD_DECISIONS} forward decisions observed",
                    lambda e: e.forward_decisions >= MIN_FORWARD_DECISIONS),
        Requirement("market_regimes",
                    f"observed in at least {MIN_MARKET_REGIMES} market regimes",
                    lambda e: e.market_regimes_observed >= MIN_MARKET_REGIMES),
        Requirement("stability", "stability checks passed",
                    lambda e: e.stability_checks_passed),
        Requirement("no_severe_drift", "no severe feature/prediction drift",
                    lambda e: not e.severe_drift_detected),
    ],
    (ModelStatus.PAPER, ModelStatus.ACTIVE): [
        Requirement("paper_sample", f"at least {MIN_PAPER_DECISIONS} paper decisions",
                    lambda e: e.paper_decisions >= MIN_PAPER_DECISIONS),
        Requirement("risk_limits_respected",
                    "risk limits measured and never breached in paper",
                    lambda e: e.risk_limit_breaches == 0),
        Requirement("execution_reliability",
                    f"execution success rate ≥ {MIN_EXECUTION_SUCCESS_RATE:.0%}",
                    lambda e: e.execution_success_rate >= MIN_EXECUTION_SUCCESS_RATE),
        Requirement("performance_measured", "paper performance recorded",
                    lambda e: e.paper_return is not None),
        Requirement("beats_benchmark",
                    "paper return beats the benchmark on the same window",
                    lambda e: (e.paper_return is not None
                               and e.benchmark_return is not None
                               and e.paper_return > e.benchmark_return)),
    ],
}


class PromotionPolicy:
    """Evaluates one promotion against the requirement table."""

    version = "promotion-policy:v1"

    def evaluate(self, *, model_id: str, current: ModelStatus, target: ModelStatus,
                 evidence: PromotionEvidenceBundle | None = None) -> PromotionDecision:
        evidence = evidence or PromotionEvidenceBundle()
        decision = PromotionDecision(
            model_id=model_id, current=current.value, target=target.value,
            approved=False, evidence=evidence.to_dict())

        # Retirement and rejection are always allowed: stopping a model is
        # never something that should need evidence to justify.
        if target in (ModelStatus.RETIRED, ModelStatus.REJECTED):
            decision.approved = True
            decision.reason = f"{target.value} needs no evidence — stopping a model is always allowed"
            return decision

        requirements = REQUIREMENTS.get((current, target))
        if requirements is None:
            decision.reason = (f"{current.value} → {target.value} is not a defined "
                               f"promotion step")
            decision.failures = [decision.reason]
            return decision

        decision.checks = [r.evaluate(evidence) for r in requirements]
        decision.failures = [c["requirement"] for c in decision.checks if not c["passed"]]
        decision.approved = not decision.failures
        decision.reason = (
            f"{len(decision.checks)} evidence requirements satisfied for "
            f"{current.value} → {target.value}"
            if decision.approved else
            f"{len(decision.failures)}/{len(decision.checks)} evidence requirements "
            f"unmet: " + "; ".join(decision.failures))
        return decision

    def requirements_for(self, current: ModelStatus,
                         target: ModelStatus) -> list[dict[str, str]]:
        return [{"name": r.name, "requirement": r.describe}
                for r in REQUIREMENTS.get((current, target), [])]

    def all_requirements(self) -> dict[str, list[dict[str, str]]]:
        return {f"{c.value}->{t.value}": [{"name": r.name, "requirement": r.describe}
                                          for r in reqs]
                for (c, t), reqs in REQUIREMENTS.items()}


def evidence_from_experiment(experiment: dict[str, Any], *,
                             artifact_hash_valid: bool = False,
                             leakage_suite_passed: bool = False,
                             dataset_pit_checks_passed: bool = False,
                             experiment_reproducible: bool = False,
                             ) -> PromotionEvidenceBundle:
    """Build the offline half of the bundle from a recorded experiment.

    The four flags are NOT inferred from the experiment file. A stored
    experiment cannot attest that a leakage suite ran — only the caller that
    actually ran it can, and it has to say so explicitly.
    """
    metrics = experiment.get("metrics") or {}
    trading = metrics.get("trading") or {}
    return PromotionEvidenceBundle(
        dataset_pit_checks_passed=dataset_pit_checks_passed,
        leakage_suite_passed=leakage_suite_passed,
        oos_folds=int(metrics.get("n_folds") or 0),
        oos_rows=int(metrics.get("oos_rows") or 0),
        experiment_reproducible=experiment_reproducible,
        artifact_hash_valid=artifact_hash_valid,
        oos_metrics=metrics.get("signal") or {},
        baseline_metrics=metrics.get("equal_weight_benchmark") or {},
        cost_adjusted_backtest=bool(metrics.get("cost_model")) and bool(trading),
        experiment_id=str(experiment.get("experiment_id") or ""),
        dataset_id=str(experiment.get("dataset_version") or ""),
    )
