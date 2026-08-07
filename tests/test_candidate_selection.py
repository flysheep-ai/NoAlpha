"""Candidate discovery must belong to the quant model when one is ACTIVE.

The bug these tests lock down: `if candidate.score < buy_score_threshold:
continue` ran unconditionally, so an ACTIVE model's #1 ranked name was dropped
before Research whenever its momentum heuristic happened to be weak. The
pipeline claimed to be `Quant → Research` while actually being
`Heuristic Momentum Gate → Quant`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from noalpha.quant.domain import Prediction, QuantSignal, SignalDirection
from noalpha.trading.candidate_policy import (
    HEURISTIC_FALLBACK, QUANT, CandidateSelectionPolicy,
)


@dataclass
class FakeCandidate:
    symbol: str
    score: float                    # the heuristic momentum score
    candidate_source: str = ""
    quant_rank: int | None = None
    quant_strength: float | None = None
    selection_reason: str = ""


def _signal(symbol: str, strength: float, direction=SignalDirection.LONG) -> QuantSignal:
    return QuantSignal(symbol=symbol, direction=direction, strength=strength,
                       confidence=strength, model_version="mdl-test")


def _prediction(symbol: str, rank: int, n: int = 10) -> Prediction:
    return Prediction(symbol=symbol, predicted_rank=rank, universe_size=n,
                      predicted_return=0.02, model_version="mdl-test")


# ── the core regression ──


def test_quant_top_candidate_survives_a_weak_heuristic_score():
    """The #1 quant name with a heuristic score far below the old 0.60 gate
    must still reach Research."""
    top = FakeCandidate("TOP", score=0.05)      # would have been filtered out
    other = FakeCandidate("MEH", score=0.95)    # heuristic darling, quant-neutral

    policy = CandidateSelectionPolicy(heuristic_threshold=0.60)
    result = policy.select(
        [top, other], quant_available=True,
        signals_by_symbol={"TOP": _signal("TOP", 0.95),
                           "MEH": _signal("MEH", 0.20, SignalDirection.NEUTRAL)},
        predictions_by_symbol={"TOP": _prediction("TOP", 1), "MEH": _prediction("MEH", 6)})

    assert result.source == QUANT
    assert [c.symbol for c in result.selected] == ["TOP"]
    assert top.candidate_source == QUANT
    assert top.quant_rank == 1
    assert result.policy["heuristic_threshold_applied"] is False


def test_heuristic_score_is_only_a_tiebreak_under_quant():
    """Equal quant strength → the heuristic decides the order. Unequal quant
    strength → the heuristic must not be able to overturn it."""
    strong_quant_weak_heuristic = FakeCandidate("A", score=0.10)
    weak_quant_strong_heuristic = FakeCandidate("B", score=0.99)
    policy = CandidateSelectionPolicy(top_k=2)
    result = policy.select(
        [weak_quant_strong_heuristic, strong_quant_weak_heuristic],
        quant_available=True,
        signals_by_symbol={"A": _signal("A", 0.90), "B": _signal("B", 0.60)},
        predictions_by_symbol={})
    assert [c.symbol for c in result.selected] == ["A", "B"]


def test_quant_short_and_neutral_names_are_not_researched():
    policy = CandidateSelectionPolicy()
    result = policy.select(
        [FakeCandidate("S", score=0.99), FakeCandidate("N", score=0.99)],
        quant_available=True,
        signals_by_symbol={"S": _signal("S", 0.05, SignalDirection.SHORT),
                           "N": _signal("N", 0.50, SignalDirection.NEUTRAL)},
        predictions_by_symbol={})
    assert result.selected == []
    reasons = {r["symbol"]: r["reason"] for r in result.rejected}
    assert reasons["S"] == "quant_not_long"
    assert reasons["N"] == "quant_not_long"


def test_instrument_without_a_quant_signal_is_rejected_not_heuristically_admitted():
    """A stale instrument must not sneak back in through the old gate — that
    would make the pipeline quant-led for some names and heuristic-led for
    others while reporting a single source."""
    policy = CandidateSelectionPolicy(heuristic_threshold=0.10)
    result = policy.select(
        [FakeCandidate("STALE", score=0.99)], quant_available=True,
        signals_by_symbol={}, predictions_by_symbol={})
    assert result.selected == []
    assert result.rejected[0]["reason"] == "no_quant_signal"


def test_top_k_bounds_the_research_budget():
    cands = [FakeCandidate(f"S{i}", score=0.5) for i in range(10)]
    signals = {c.symbol: _signal(c.symbol, 0.9 - i * 0.01)
               for i, c in enumerate(cands)}
    result = CandidateSelectionPolicy(top_k=3).select(
        cands, quant_available=True, signals_by_symbol=signals,
        predictions_by_symbol={})
    assert [c.symbol for c in result.selected] == ["S0", "S1", "S2"]
    assert all(r["reason"] == "below_top_k" for r in result.rejected)


# ── fallback ──


def test_heuristic_fallback_engages_only_when_quant_is_unavailable():
    hot, cold = FakeCandidate("HOT", score=0.80), FakeCandidate("COLD", score=0.30)
    result = CandidateSelectionPolicy(heuristic_threshold=0.60).select(
        [hot, cold], quant_available=False, quant_error="no ACTIVE model in registry")

    assert result.source == HEURISTIC_FALLBACK
    assert [c.symbol for c in result.selected] == ["HOT"]
    assert hot.candidate_source == HEURISTIC_FALLBACK
    assert hot.quant_rank is None
    assert result.policy["heuristic_threshold_applied"] is True
    assert "no ACTIVE model in registry" in result.policy["quant_error"]


def test_selection_provenance_is_serialisable_for_system_truth():
    result = CandidateSelectionPolicy().select(
        [FakeCandidate("X", score=0.9)], quant_available=False)
    payload = result.to_dict()
    assert payload["source"] == HEURISTIC_FALLBACK
    assert payload["selected"][0]["candidate_source"] == HEURISTIC_FALLBACK
    assert payload["policy"]["gate"] == "heuristic_score"
