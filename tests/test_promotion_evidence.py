"""Promotion must cost evidence, not five API calls.

The old gate checked one thing — "is this the adjacent stage?" — so a model
could reach ACTIVE with no out-of-sample folds, no leakage suite, no benchmark
and no forward observation. The lifecycle diagram implied rigour the code did
not implement.
"""
from __future__ import annotations

import pytest

from noalpha.quant.domain import ModelStatus, ModelVersion
from noalpha.quant.models import create_model
from noalpha.quant.promotion import (
    MIN_OOS_FOLDS, MIN_OOS_ROWS, PromotionEvidenceBundle, PromotionPolicy,
    evidence_from_experiment,
)
from noalpha.quant.registry import PromotionError, QuantModelRegistry

from tests.test_quant_core import make_history
from noalpha.quant.dataset import QuantDatasetBuilder


# ── evidence bundles ──


def _validated_evidence(**over) -> PromotionEvidenceBundle:
    base = dict(dataset_pit_checks_passed=True, leakage_suite_passed=True,
                oos_folds=MIN_OOS_FOLDS, oos_rows=MIN_OOS_ROWS,
                experiment_reproducible=True, artifact_hash_valid=True)
    return PromotionEvidenceBundle(**{**base, **over})


def _shadow_evidence(**over) -> PromotionEvidenceBundle:
    base = dict(oos_metrics={"rank_ic": 0.03}, baseline_metrics={"sharpe": 0.4},
                cost_adjusted_backtest=True, critical_data_quality_warnings=[])
    return PromotionEvidenceBundle(**{**base, **over})


def _paper_evidence(**over) -> PromotionEvidenceBundle:
    base = dict(forward_shadow_cycles=25, forward_decisions=40,
                market_regimes_observed=2, stability_checks_passed=True,
                severe_drift_detected=False)
    return PromotionEvidenceBundle(**{**base, **over})


def _active_evidence(**over) -> PromotionEvidenceBundle:
    base = dict(paper_decisions=40, risk_limit_breaches=0,
                execution_success_rate=0.99, paper_return=0.08,
                benchmark_return=0.03)
    return PromotionEvidenceBundle(**{**base, **over})


# ── policy ──


def test_an_empty_bundle_fails_every_stage():
    """Evidence absent is not evidence passed."""
    policy = PromotionPolicy()
    for current, target in [(ModelStatus.EXPERIMENTAL, ModelStatus.VALIDATED),
                            (ModelStatus.VALIDATED, ModelStatus.SHADOW),
                            (ModelStatus.SHADOW, ModelStatus.PAPER),
                            (ModelStatus.PAPER, ModelStatus.ACTIVE)]:
        d = policy.evaluate(model_id="m", current=current, target=target)
        assert not d.approved, f"{current.value} → {target.value} passed on no evidence"
        assert d.failures
        # Every requirement that asserts something POSITIVE was measured must
        # fail. ("no outstanding critical warning" is legitimately satisfied by
        # an empty bundle — absence of a warning is not a claim about the model.)
        positive = [c for c in d.checks if not c["name"].startswith("no_")]
        assert all(not c["passed"] for c in positive)


@pytest.mark.parametrize("missing", [
    "dataset_pit_checks_passed", "leakage_suite_passed",
    "experiment_reproducible", "artifact_hash_valid",
])
def test_a_single_missing_validation_requirement_denies_promotion(missing):
    d = PromotionPolicy().evaluate(
        model_id="m", current=ModelStatus.EXPERIMENTAL, target=ModelStatus.VALIDATED,
        evidence=_validated_evidence(**{missing: False}))
    assert not d.approved
    assert len(d.failures) == 1


def test_pit_or_leakage_failure_specifically_blocks_validation():
    d = PromotionPolicy().evaluate(
        model_id="m", current=ModelStatus.EXPERIMENTAL, target=ModelStatus.VALIDATED,
        evidence=_validated_evidence(leakage_suite_passed=False,
                                     dataset_pit_checks_passed=False))
    assert not d.approved
    assert any("leakage" in f for f in d.failures)
    assert any("point-in-time" in f for f in d.failures)


def test_thin_out_of_sample_evidence_is_rejected():
    d = PromotionPolicy().evaluate(
        model_id="m", current=ModelStatus.EXPERIMENTAL, target=ModelStatus.VALIDATED,
        evidence=_validated_evidence(oos_folds=1, oos_rows=10))
    assert not d.approved
    assert len(d.failures) == 2


def test_shadow_requires_a_cost_adjusted_backtest_and_a_baseline():
    policy = PromotionPolicy()
    assert policy.evaluate(model_id="m", current=ModelStatus.VALIDATED,
                           target=ModelStatus.SHADOW,
                           evidence=_shadow_evidence()).approved
    assert not policy.evaluate(
        model_id="m", current=ModelStatus.VALIDATED, target=ModelStatus.SHADOW,
        evidence=_shadow_evidence(cost_adjusted_backtest=False)).approved
    assert not policy.evaluate(
        model_id="m", current=ModelStatus.VALIDATED, target=ModelStatus.SHADOW,
        evidence=_shadow_evidence(baseline_metrics={})).approved
    assert not policy.evaluate(
        model_id="m", current=ModelStatus.VALIDATED, target=ModelStatus.SHADOW,
        evidence=_shadow_evidence(
            critical_data_quality_warnings=["survivorship"])).approved


def test_paper_requires_real_forward_observation():
    policy = PromotionPolicy()
    assert policy.evaluate(model_id="m", current=ModelStatus.SHADOW,
                           target=ModelStatus.PAPER,
                           evidence=_paper_evidence()).approved
    # a model registered five minutes ago has no forward evidence at all
    fresh = policy.evaluate(model_id="m", current=ModelStatus.SHADOW,
                            target=ModelStatus.PAPER,
                            evidence=PromotionEvidenceBundle())
    assert not fresh.approved
    assert not policy.evaluate(
        model_id="m", current=ModelStatus.SHADOW, target=ModelStatus.PAPER,
        evidence=_paper_evidence(severe_drift_detected=True)).approved
    assert not policy.evaluate(
        model_id="m", current=ModelStatus.SHADOW, target=ModelStatus.PAPER,
        evidence=_paper_evidence(market_regimes_observed=1)).approved


def test_active_requires_beating_the_benchmark():
    policy = PromotionPolicy()
    assert policy.evaluate(model_id="m", current=ModelStatus.PAPER,
                           target=ModelStatus.ACTIVE,
                           evidence=_active_evidence()).approved
    losing = policy.evaluate(
        model_id="m", current=ModelStatus.PAPER, target=ModelStatus.ACTIVE,
        evidence=_active_evidence(paper_return=0.01, benchmark_return=0.05))
    assert not losing.approved
    assert any("benchmark" in f for f in losing.failures)


def test_unmeasured_risk_and_execution_are_failures_not_passes():
    """`risk_limit_breaches=-1` means "never measured". It must not read as
    "zero breaches"."""
    policy = PromotionPolicy()
    d = policy.evaluate(model_id="m", current=ModelStatus.PAPER,
                        target=ModelStatus.ACTIVE,
                        evidence=_active_evidence(risk_limit_breaches=-1,
                                                  execution_success_rate=-1.0))
    assert not d.approved
    assert len(d.failures) == 2


def test_retirement_never_needs_evidence():
    for target in (ModelStatus.RETIRED, ModelStatus.REJECTED):
        d = PromotionPolicy().evaluate(model_id="m", current=ModelStatus.ACTIVE,
                                       target=target)
        assert d.approved


# ── registry integration ──


def _register(tmp_path):
    reg = QuantModelRegistry(tmp_path)
    ds = QuantDatasetBuilder().build(make_history(), "2023-06-01", "2024-02-01")
    m = create_model("linear_ridge")
    m.fit(ds.frame[ds.feature_columns], ds.frame["label"])
    v = reg.register(ModelVersion(name="linear_ridge", feature_ids=ds.feature_columns,
                                  label_id="excess_return_5d:v1"), m)
    return reg, v


def test_registry_refuses_promotion_without_evidence(tmp_path):
    reg, v = _register(tmp_path)
    with pytest.raises(PromotionError, match="evidence gate"):
        reg.promote(v.model_id, ModelStatus.VALIDATED)
    assert reg.get(v.model_id)["status"] == ModelStatus.EXPERIMENTAL.value


def test_registry_still_refuses_to_skip_a_stage_even_with_perfect_evidence(tmp_path):
    reg, v = _register(tmp_path)
    with pytest.raises(PromotionError, match="skips stages"):
        reg.promote(v.model_id, ModelStatus.ACTIVE, _active_evidence())


def test_a_model_without_forward_evidence_stays_in_shadow(tmp_path):
    """The whole point. A freshly trained model with excellent offline
    numbers reaches SHADOW and stops there."""
    reg, v = _register(tmp_path)
    reg.promote(v.model_id, ModelStatus.VALIDATED, _validated_evidence())
    reg.promote(v.model_id, ModelStatus.SHADOW, _shadow_evidence())
    with pytest.raises(PromotionError, match="evidence gate"):
        reg.promote(v.model_id, ModelStatus.PAPER, PromotionEvidenceBundle())
    assert reg.get(v.model_id)["status"] == ModelStatus.SHADOW.value
    assert reg.active_model() is None


def test_the_full_evidenced_path_reaches_active(tmp_path):
    reg, v = _register(tmp_path)
    for target, evidence in [(ModelStatus.VALIDATED, _validated_evidence()),
                             (ModelStatus.SHADOW, _shadow_evidence()),
                             (ModelStatus.PAPER, _paper_evidence()),
                             (ModelStatus.ACTIVE, _active_evidence())]:
        reg.promote(v.model_id, target, evidence)
    assert reg.get(v.model_id)["status"] == ModelStatus.ACTIVE.value


def test_every_promotion_records_the_decision_it_was_made_on(tmp_path):
    reg, v = _register(tmp_path)
    record = reg.promote(v.model_id, ModelStatus.VALIDATED, _validated_evidence())
    decision = record["status_history"][-1]["promotion_decision"]
    assert decision["approved"] is True
    assert len(decision["checks"]) == 6
    assert decision["evidence"]["oos_folds"] == MIN_OOS_FOLDS


def test_an_override_is_recorded_not_hidden(tmp_path):
    reg, v = _register(tmp_path)
    record = reg.promote(v.model_id, ModelStatus.VALIDATED,
                         override_reason="operator forced for a smoke test")
    decision = record["last_promotion_decision"]
    assert decision["override"] is True
    assert decision["override_reason"] == "operator forced for a smoke test"
    assert decision["failures"], "the bypassed requirements must stay in the record"


def test_artifact_hash_mismatch_blocks_loading_the_model(tmp_path):
    """A tampered artifact must never reach inference, promoted or not."""
    reg, v = _register(tmp_path)
    for target, evidence in [(ModelStatus.VALIDATED, _validated_evidence()),
                             (ModelStatus.SHADOW, _shadow_evidence()),
                             (ModelStatus.PAPER, _paper_evidence()),
                             (ModelStatus.ACTIVE, _active_evidence())]:
        reg.promote(v.model_id, target, evidence)
    artifact = tmp_path / f"{v.model_id}.pkl"
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    with pytest.raises(PromotionError, match="artifact hash mismatch"):
        reg.active_model()


def test_requirements_are_introspectable(tmp_path):
    reg, v = _register(tmp_path)
    req = reg.promotion_requirements(v.model_id)
    assert req["current"] == "experimental"
    assert req["next"] == "validated"
    assert {r["name"] for r in req["requirements"]} >= {"leakage_suite", "oos_folds"}


def test_evidence_from_experiment_does_not_invent_attestations():
    """A stored experiment cannot certify that a leakage suite ran."""
    exp = {"experiment_id": "exp-1", "dataset_version": "ds-1",
           "metrics": {"n_folds": 5, "oos_rows": 900, "signal": {"rank_ic": 0.02},
                       "equal_weight_benchmark": {"sharpe": 0.3},
                       "cost_model": {"bps": 15}, "trading": {"net_return": 0.1}}}
    e = evidence_from_experiment(exp)
    assert e.oos_folds == 5 and e.oos_rows == 900
    assert e.cost_adjusted_backtest is True
    # not inferable from a file — must stay False unless the caller attests
    assert e.leakage_suite_passed is False
    assert e.dataset_pit_checks_passed is False
    assert e.experiment_reproducible is False
    assert e.artifact_hash_valid is False
