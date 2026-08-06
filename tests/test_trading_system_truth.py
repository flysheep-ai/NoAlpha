"""System-truth guarantees for the autonomous loop.

These are the "would this test still pass if the bug came back?" tests
(rule 70). Each asserts a real behavioral invariant, not a status code:

  1. an executed proposal is NEVER readable as 'pending';
  2. the same instrument is not re-proposed every cycle (dedup + cooldown);
  3. the effective decision mode reflects REAL component availability;
  4. system_truth() reports quant UNAVAILABLE honestly when features are stale;
  5. theses survive a loop restart (persisted, not memory-only).
"""
from __future__ import annotations

from noalpha.trading.autonomous import AutonomousTradingLoop
from noalpha.trading.session import SessionStatus, TradingStore

from tests.test_quant_trading_integration import build_world, events_of


def test_executed_proposal_never_pending(tmp_path):
    loop, store, broker, session = build_world(tmp_path)
    summary = loop.run_cycle()
    assert summary["actions"], "precondition: a BUY must execute"
    executed_symbols = {a["symbol"] for a in summary["actions"] if a["action"] == "buy"}

    proposals = store.list_proposals(session.session_id)
    assert proposals, "proposals table must be populated"
    # Every proposal for an executed symbol reads 'executed' — not 'pending'.
    for p in proposals:
        if p["symbol"] in executed_symbols and p["action"] == "buy":
            assert p["status"] == "executed", (
                f"{p['symbol']} proposal still {p['status']!r} after a fill")
    # And the active-proposal dedup query returns nothing for executed symbols.
    for sym in executed_symbols:
        assert store.active_proposal(session.session_id, sym) is None


def test_no_duplicate_proposal_across_cycles(tmp_path):
    loop, store, broker, session = build_world(tmp_path)
    loop.run_cycle()
    loop.run_cycle()
    loop.run_cycle()
    # Count BUY proposals per symbol in the first-class table.
    proposals = store.list_proposals(session.session_id, limit=500)
    by_symbol: dict[str, int] = {}
    for p in proposals:
        if p["action"] == "buy":
            by_symbol[p["symbol"]] = by_symbol.get(p["symbol"], 0) + 1
    # A symbol that was bought must not be re-proposed while it holds a position
    # and is within the cooldown window.
    for sym, count in by_symbol.items():
        assert count == 1, f"{sym} proposed {count}× across 3 cycles (dedup/cooldown broken)"


def test_effective_mode_is_quant_deterministic_without_llm(tmp_path):
    loop, store, broker, session = build_world(tmp_path)
    assert session.research_mode != "llm"  # test env has no LLM
    summary = loop.run_cycle()
    # Quant model is ACTIVE in build_world → quant_deterministic, never a fake hybrid.
    assert summary["effective_mode"] == "quant_deterministic"
    truth = loop.system_truth()
    assert truth["effective_mode"] == "quant_deterministic"
    assert truth["components"]["quant"]["status"] == "HEALTHY"
    assert truth["components"]["llm"]["status"] == "UNAVAILABLE"


def test_system_truth_reports_quant_unavailable_when_stale(tmp_path):
    """If quant produces no fresh features, the loop must fall back to a pure
    deterministic mode AND system_truth must say quant is UNAVAILABLE — it may
    never pretend the model participated."""
    loop, store, broker, session = build_world(tmp_path)

    # Break quant: force the inference service to error like the real
    # "no instrument produced fresh features" case.
    from noalpha.quant.inference import InferenceResult

    class DeadQuant:
        last_result = None
        def infer(self, *a, **k):
            self.last_result = InferenceResult(
                as_of="2023-01-01", error="no instrument produced fresh features",
                skipped=["105.SYM0: features stale"])
            return self.last_result

    loop.quant = DeadQuant()
    summary = loop.run_cycle()
    assert summary["effective_mode"] == "deterministic_signal"
    truth = loop.system_truth()
    assert truth["components"]["quant"]["status"] == "UNAVAILABLE"
    assert "fresh features" in truth["components"]["quant"]["detail"]


def test_theses_persist_across_restart(tmp_path):
    loop, store, broker, session = build_world(tmp_path)
    summary = loop.run_cycle()
    assert summary["actions"], "precondition: a BUY must execute"
    assert loop.theses, "in-memory theses must exist after a buy"

    # A brand-new loop over the SAME store must rehydrate the theses.
    fresh_store = TradingStore(tmp_path / "trading.sqlite3")
    reloaded = AutonomousTradingLoop(session, fresh_store, broker,
                                     feed=loop.feed, universe_provider=lambda: [])
    assert reloaded.theses, "theses did not survive restart (memory-only bug)"
    # thesis text round-tripped
    any_thesis = next(iter(reloaded.theses.values()))
    assert any_thesis.thesis_text
