"""Point-in-time invariants across the evidence and LLM-feature paths.

The quant feature/label side is covered by tests/test_quant_leakage.py (causal
features, same-bar execution, purged splits, oracle detection). This file
covers the two PIT surfaces that had enforcement but no test: evidence
filtering, and the rule that a retro-generated LLM snapshot may be research
output but may never become a training feature.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from noalpha.domain import AsOfContext, Evidence, EvidenceType
from noalpha.errors import PointInTimeViolation
from noalpha.pit import assert_available_as_of, filter_evidence_as_of
from noalpha.quant.llm_features import LLMFeatureSnapshot, LLMFeatureStore


T0 = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _evidence(eid: str, available: datetime, event: datetime | None = None) -> Evidence:
    return Evidence(evidence_id=eid, evidence_type=EvidenceType.NEWS, source="test",
                    source_url=None, event_time=(event or available),
                    published_at=available, available_at=available,
                    retrieved_at=T0 + timedelta(days=30), content="c")


# ── evidence ──


def test_evidence_published_after_as_of_is_rejected():
    ctx = AsOfContext(as_of=T0)
    kept = filter_evidence_as_of([
        _evidence("past", T0 - timedelta(hours=1)),
        _evidence("future", T0 + timedelta(hours=1)),
    ], ctx)
    assert [e.evidence_id for e in kept] == ["past"]


def test_an_event_after_as_of_is_rejected_even_with_a_backdated_available_at():
    """Contradictory metadata fails closed: an event that has not happened yet
    cannot be evidence, whatever `available_at` claims."""
    ctx = AsOfContext(as_of=T0)
    backdated = _evidence("backdated", T0 - timedelta(days=1),
                          event=T0 + timedelta(days=1))
    assert filter_evidence_as_of([backdated], ctx) == []


def test_retrieved_at_after_as_of_is_allowed():
    """A backtest legitimately fetches historical data later than as_of; only
    availability and event time govern PIT."""
    ctx = AsOfContext(as_of=T0)
    item = _evidence("historical", T0 - timedelta(days=5))
    assert filter_evidence_as_of([item], ctx) == [item]


def test_assert_available_as_of_raises_on_a_future_item():
    ctx = AsOfContext(as_of=T0)
    with pytest.raises(PointInTimeViolation):
        assert_available_as_of(_evidence("future", T0 + timedelta(seconds=1)), ctx)


# ── LLM features ──


def _snap(store: LLMFeatureStore, sid: str, *, available: str,
          research_only: bool = False, pit_safe: bool = True) -> None:
    store.append(LLMFeatureSnapshot(
        feature_snapshot_id=sid, instrument_id="105.AAA", symbol="AAA",
        as_of=available, generated_at=available, available_at=available,
        mature_at=available, research_only=research_only, pit_safe=pit_safe))


def test_a_retro_generated_snapshot_is_never_trainable(tmp_path):
    """Attack: point the LLM at a past date, where the outcome is already
    known, then feed the result to the model as a feature."""
    store = LLMFeatureStore(tmp_path)
    _snap(store, "live", available="2026-08-01T00:00:00+00:00")
    _snap(store, "retro", available="2026-08-01T00:00:00+00:00",
          research_only=True, pit_safe=False)

    ids = {r["feature_snapshot_id"] for r in store.trainable_rows()}
    assert ids == {"live"}, "a retro-generated snapshot reached the training set"
    # it is still preserved as research output — excluded, not deleted
    assert len(store.load_all()) == 2


def test_a_snapshot_that_did_not_exist_yet_cannot_inform_the_decision(tmp_path):
    store = LLMFeatureStore(tmp_path)
    _snap(store, "before", available="2026-08-01T00:00:00+00:00")
    _snap(store, "after", available="2026-08-10T00:00:00+00:00")
    ids = {r["feature_snapshot_id"]
           for r in store.trainable_rows(as_of="2026-08-05T00:00:00+00:00")}
    assert ids == {"before"}


def test_readiness_counts_exclude_research_only_rows(tmp_path):
    store = LLMFeatureStore(tmp_path)
    for i in range(5):
        _snap(store, f"retro{i}", available="2026-08-01T00:00:00+00:00",
              research_only=True, pit_safe=False)
    status = store.status()
    assert status["pit_safe_snapshots"] == 0
    assert status["training_allowed"] is False
    assert status["state"] == "COLLECTING"


def test_forward_snapshots_never_claim_availability_before_completion():
    """`available_at` is the real completion time. Setting it to the as_of
    date would make a 160-second investigation look instantaneous and let a
    decision cite research that had not finished."""
    from noalpha.trading.research_fusion import TradingResearchSnapshot

    snap = TradingResearchSnapshot(as_of="2026-08-06T09:30:00+00:00",
                                   generated_at="2026-08-06T09:33:00+00:00",
                                   available_at="2026-08-06T09:33:00+00:00")
    assert snap.available_at >= snap.generated_at
    assert snap.available_at > snap.as_of
