"""Adversarial checks for admitting AI research into executable decisions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pytest

from noalpha.quant.domain import QuantSignal, SignalDirection
from noalpha.trading.research_fusion import (
    FusionPolicy, LLMCallBudget, LLMResearchSignal, ResearchDirection,
    ResearchFreshnessPolicy, TradingResearchService, TradingResearchSnapshot,
    snapshot_to_signal,
)


NOW = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)


def snapshot(**changes):
    return replace(TradingResearchSnapshot(
        instrument_id="105.AAPL", symbol="AAPL", market="US",
        as_of=(NOW - timedelta(minutes=2)).isoformat(),
        generated_at=(NOW - timedelta(minutes=1)).isoformat(),
        available_at=(NOW - timedelta(minutes=1)).isoformat(),
        research_mode="llm", thesis_direction=ResearchDirection.STRONGLY_BULLISH,
        bull_strength=0.9, bear_strength=0.1, thesis_confidence=0.8,
        selected_evidence_count=12, citation_support=0.9,
        input_tokens=100, output_tokens=50, latency_ms=200,
    ), **changes)


def quant(strength=0.8, direction=SignalDirection.LONG):
    return QuantSignal(direction=direction, strength=strength)


@pytest.mark.parametrize("direction", list(ResearchDirection))
def test_ai_cannot_turn_a_weak_quant_signal_into_an_entry(direction):
    decision = FusionPolicy().fuse(
        symbol="AAPL", secid="105.AAPL", quant=quant(0.59),
        llm=LLMResearchSignal(direction=direction, confidence=0.9))
    assert decision.final_signal not in {"buy", "increase"}


def test_unsubstantiated_internal_conflict_cannot_force_liquidation():
    decision = FusionPolicy().fuse(
        symbol="AAPL", secid="105.AAPL", quant=quant(), holding=True,
        llm=LLMResearchSignal(direction=ResearchDirection.UNCERTAIN, low_evidence=True))
    assert decision.final_signal == "hold"
    assert decision.effective_mode == "quant_only_fallback"


@pytest.mark.parametrize("changes", [{"pit_safe": False}, {"research_only": True}])
def test_historical_research_cannot_veto_an_entry(changes):
    signal = snapshot_to_signal(snapshot(
        thesis_direction=ResearchDirection.STRONGLY_BEARISH, **changes))
    decision = FusionPolicy().fuse(symbol="AAPL", secid="105.AAPL", quant=quant(), llm=signal)
    assert decision.final_signal == "buy"
    assert decision.final_strength == 0.75
    assert decision.effective_mode == "quant_only_fallback"


@pytest.mark.parametrize("strength", [float("nan"), float("inf"), -1.0, 1.1])
def test_invalid_quant_strength_cannot_open_a_position(strength):
    decision = FusionPolicy().fuse(
        symbol="AAPL", secid="105.AAPL", quant=quant(strength),
        llm=LLMResearchSignal(direction=ResearchDirection.STRONGLY_BULLISH, confidence=0.9))
    assert decision.final_signal != "buy"
    json.dumps(decision.to_dict(), allow_nan=False)


@pytest.mark.parametrize("changes", [
    {"generated_at": (NOW + timedelta(minutes=1)).isoformat(),
     "available_at": (NOW + timedelta(minutes=1)).isoformat()},
    {"available_at": (NOW + timedelta(minutes=1)).isoformat()},
    {"generated_at": "2026-10-29T07:59:00"},
    {"available_at": ""},
    {"generated_at": None},
    {"as_of": (NOW - timedelta(hours=8)).isoformat()},
    {"as_of": NOW.isoformat()},
    {"pit_safe": False},
    {"research_only": True},
    {"research_mode": "deterministic"},
])
def test_unusable_research_is_never_fresh(changes):
    assert not ResearchFreshnessPolicy().is_fresh(snapshot(**changes), now=NOW)


def test_current_research_with_offset_is_reusable():
    snap = snapshot(available_at="2026-10-29T15:59:00+08:00")
    assert ResearchFreshnessPolicy().is_fresh(snap, now=NOW)


@pytest.mark.parametrize("day_change,turned_short", [(0, False), (6, False), (0, True)])
def test_denied_refresh_never_returns_invalidated_research(monkeypatch, day_change, turned_short):
    monkeypatch.setattr("noalpha.trading.research_fusion._now", lambda: NOW)
    service = TradingResearchService(lambda: pytest.fail("budget must prevent the call"),
                                    model="test", llm_provider="test",
                                    budget=LLMCallBudget(max_calls_per_cycle=0))
    snap = snapshot()
    if not day_change and not turned_short:
        snap = replace(snap, as_of=(NOW - timedelta(hours=8)).isoformat(),
                       generated_at=(NOW - timedelta(hours=7)).isoformat(),
                       available_at=(NOW - timedelta(hours=7)).isoformat())
    service.cache["105.AAPL"] = snap
    result, status = service.research(secid="105.AAPL", symbol="AAPL", market="US",
                                      day_change_pct=day_change, quant_turned_short=turned_short)
    assert result is None
    assert status.startswith("budget_denied:")
    assert "research_unavailable=" in status


def test_fresh_cache_reuse_needs_no_budget(monkeypatch):
    monkeypatch.setattr("noalpha.trading.research_fusion._now", lambda: NOW)
    service = TradingResearchService(lambda: pytest.fail("no new research needed"),
                                    model="test", llm_provider="test",
                                    budget=LLMCallBudget(max_calls_per_cycle=0))
    service.cache["105.AAPL"] = snapshot()
    result, status = service.research(secid="105.AAPL", symbol="AAPL", market="US")
    assert result is service.cache["105.AAPL"]
    assert status == "fresh_reuse"


@pytest.mark.parametrize("changes", [{}, {"pit_safe": False},
                                     {"as_of": (NOW - timedelta(hours=8)).isoformat()}])
def test_new_research_is_checked_before_caching_and_feature_collection(monkeypatch, changes):
    monkeypatch.setattr("noalpha.trading.research_fusion._now", lambda: NOW)
    monkeypatch.setattr("noalpha.trading.research_fusion.extract_snapshot",
                        lambda *a, **kw: snapshot(**changes))
    stored = []
    pipeline = SimpleNamespace(investigate=lambda *a, **kw: object(), research=SimpleNamespace())
    service = TradingResearchService(lambda: pipeline, model="test", llm_provider="test",
                                    feature_store=SimpleNamespace(append_from_trading_snapshot=stored.append))
    result, status = service.research(secid="105.AAPL", symbol="AAPL", market="US")
    if changes:
        assert result is None
        assert status.startswith("failed:research_unavailable=")
        assert not service.cache
        assert not stored
    else:
        assert status == "generated"
        assert result is service.cache["105.AAPL"]
        assert stored == [result]
    assert service.budget.cycle_calls == 1


def test_failed_refresh_cannot_resurrect_stale_research(monkeypatch):
    monkeypatch.setattr("noalpha.trading.research_fusion._now", lambda: NOW)

    def unavailable():
        raise RuntimeError("provider unavailable")

    service = TradingResearchService(unavailable, model="test", llm_provider="test")
    service.cache["105.AAPL"] = snapshot(as_of=(NOW - timedelta(hours=8)).isoformat())
    result, status = service.research(secid="105.AAPL", symbol="AAPL", market="US")
    assert result is None
    assert status.startswith("failed:")
    assert service.budget.cycle_calls == 1


@pytest.mark.parametrize("direction,multiplier", [
    (ResearchDirection.STRONGLY_BULLISH, 1.15),
    (ResearchDirection.BULLISH, 1.0),
    (ResearchDirection.NEUTRAL, 0.85),
    (ResearchDirection.BEARISH, 0.5),
    (ResearchDirection.UNCERTAIN, 0.5),
])
def test_admitted_research_still_adjusts_eligible_entries(direction, multiplier):
    decision = FusionPolicy().fuse(
        symbol="AAPL", secid="105.AAPL", quant=quant(0.6),
        llm=LLMResearchSignal(direction=direction, confidence=0.9))
    assert decision.final_signal == "buy"
    assert decision.final_strength == multiplier
    assert decision.effective_mode == "hybrid_llm_quant"
    assert decision.policy_version == "fusion-policy:v2"


def test_entry_threshold_does_not_stop_existing_position_management():
    decision = FusionPolicy().fuse(
        symbol="AAPL", secid="105.AAPL", quant=quant(0.2), holding=True,
        llm=LLMResearchSignal(direction=ResearchDirection.STRONGLY_BEARISH, confidence=0.9))
    assert decision.final_signal == "sell"


@pytest.mark.parametrize("direction", list(ResearchDirection))
def test_without_quant_research_cannot_open_a_position(direction):
    decision = FusionPolicy().fuse(symbol="AAPL", secid="105.AAPL", quant=None,
                                  llm=LLMResearchSignal(direction=direction, confidence=0.9))
    assert decision.final_signal in {"watch", "no_action"}


def test_invalid_ai_confidence_cannot_veto_a_quant_entry():
    decision = FusionPolicy().fuse(
        symbol="AAPL", secid="105.AAPL", quant=quant(),
        llm=LLMResearchSignal(direction=ResearchDirection.STRONGLY_BEARISH, confidence=float("nan")))
    assert decision.final_signal == "buy"
    assert decision.effective_mode == "quant_only_fallback"
    assert decision.llm_contribution["rejection_reason"] == "invalid_scores"
    json.dumps(decision.to_dict(), allow_nan=False)


def test_ai_veto_reaches_the_real_broker(tmp_path):
    from tests.test_quant_trading_integration import build_world, events_of

    loop, store, broker, session = build_world(tmp_path)
    loop.trading_research = SimpleNamespace(
        budget=LLMCallBudget(),
        research=lambda **kw: (snapshot(
            instrument_id=kw["secid"], symbol=kw["symbol"],
            thesis_direction=ResearchDirection.STRONGLY_BEARISH,
            bull_strength=0.1, bear_strength=0.9, negative_risk_score=0.8), "generated"))
    loop.run_cycle()
    fusions = events_of(store, session.session_id, "fusion")
    assert fusions
    assert all(e["payload"]["final_signal"] == "no_action" for e in fusions)
    # Arm B is still a useful independent counterfactual, but must not execute
    # a buy behind Arm C's back after the active fusion policy vetoes it.
    assert any(a["deterministic_hybrid"].action == "buy" for a in loop._arm_decisions.values())
    assert not broker.positions
    assert not events_of(store, session.session_id, "proposal")


@pytest.mark.parametrize("status", ["fresh_reuse", "budget_denied:cycle budget exhausted"])
def test_reuse_and_budget_denial_are_not_new_llm_calls(tmp_path, status):
    from tests.test_quant_trading_integration import build_world, events_of

    loop, store, _broker, session = build_world(tmp_path)
    loop.trading_research = SimpleNamespace(
        budget=LLMCallBudget(),
        research=lambda **kw: (snapshot() if status == "fresh_reuse" else None, status))
    loop.run_cycle()
    assert events_of(store, session.session_id, "fusion")
    assert loop.shadow.llm_stats["calls"] == 0
    assert loop.shadow.llm_stats["tokens"] == 0
    assert loop.shadow.llm_stats["failures"] == 0
    if status.startswith("budget_denied"):
        assert not any(d["llm_changed_decision"] for d in loop.shadow.load_deltas())
        assert loop.shadow.decision_attribution()["llm_confirmation_count"] == 0
        assert loop.shadow.llm_stats["vetoes"] == 0
        assert loop.shadow.llm_stats["confirmations"] == 0
        for event in events_of(store, session.session_id, "proposal"):
            assert event["payload"]["proposal"]["decision_mode"] == "quant_only_fallback"
