"""End-to-end measurement integrity, plus a deliberate attack suite.

Part 1 asserts the P0 fixes hold inside the REAL AutonomousTradingLoop, not
just in their own modules — a unit-tested component wired up wrong is still a
broken measurement.

Part 2 attacks the system on purpose. Each test tries to make NoAlpha report a
number it has not earned: leak the future into a shadow return, slip a
heuristic gate in front of an ACTIVE quant model, let an LLM route around hard
risk, trade on a stale quote, reset a daily loss limit by restarting, promote
an unvalidated model. Anything that succeeds is a bug, so every one of these
asserts the attack FAILS.
"""
from __future__ import annotations

import pytest

from noalpha.quant.domain import SignalDirection
from noalpha.trading.candidate_policy import HEURISTIC_FALLBACK, QUANT
from noalpha.trading.proposal import TradeAction, TradeProposal
from noalpha.trading.shadow import ARMS, ArmDecision, ShadowPortfolio
from noalpha.trading.trading_agents import TradingAgentsOrchestrator

from tests.test_quant_trading_integration import build_world, events_of


# ══════════════════════════════════════════════════════════════════════
# Part 1 — the fixes hold in the real loop
# ══════════════════════════════════════════════════════════════════════


def test_candidate_selection_is_quant_led_in_the_real_loop(tmp_path):
    loop, store, _broker, session = build_world(tmp_path)
    loop.run_cycle()

    selection = events_of(store, session.session_id, "selection")
    assert selection, "no candidate-selection provenance was emitted"
    payload = selection[-1]["payload"]
    assert payload["source"] == QUANT
    assert payload["policy"]["heuristic_threshold_applied"] is False
    assert payload["policy"]["gate"] == "quant_rank"

    for cand in events_of(store, session.session_id, "candidate"):
        assert cand["payload"]["candidate_source"] == QUANT


def test_a_top_quant_name_with_a_weak_heuristic_score_still_reaches_research(tmp_path):
    """The headline regression: the heuristic gate must not be able to veto
    the ACTIVE model's top-ranked instrument."""
    loop, store, _broker, session = build_world(tmp_path)

    # Crush every heuristic score below the old 0.60 buy threshold while
    # leaving the quant ranking untouched.
    original_scan = loop.engine.scan_universe

    def flattened(universe, feed):
        candidates, skipped = original_scan(universe, feed)
        for c in candidates:
            c.score = 0.01
        return candidates, skipped

    loop.engine.scan_universe = flattened
    loop.run_cycle()

    researched = events_of(store, session.session_id, "candidate")
    assert researched, (
        "every candidate was filtered out by the heuristic score even though an "
        "ACTIVE quant model ranked the universe")
    assert all(e["payload"]["score"] < 0.60 for e in researched)
    assert all(e["payload"]["candidate_source"] == QUANT for e in researched)


def test_heuristic_fallback_engages_when_quant_is_unavailable(tmp_path):
    from noalpha.quant.inference import InferenceResult, QuantInferenceService

    loop, store, _broker, session = build_world(tmp_path)

    class DeadQuant(QuantInferenceService):
        def infer(self, *a, **kw):
            return InferenceResult(error="no ACTIVE model in registry")

    loop.quant = DeadQuant(loop.quant.registry)
    loop.run_cycle()

    payload = events_of(store, session.session_id, "selection")[-1]["payload"]
    assert payload["source"] == HEURISTIC_FALLBACK
    assert payload["policy"]["heuristic_threshold_applied"] is True


def test_the_quant_analyst_role_reads_the_real_model_not_the_heuristic(tmp_path):
    """`agents.run()` was called without quant_view, so the role called itself
    quant_analyst while reading `opportunity_score`."""
    loop, store, _broker, session = build_world(tmp_path)
    loop.run_cycle()

    reports = events_of(store, session.session_id, "agent")
    assert reports
    analysts = {a["role"]: a for a in reports[-1]["payload"]["analysts"]}
    quant_analyst = analysts["quant_analyst"]
    assert quant_analyst["status"] == "available", \
        "the quant analyst is still degraded to the heuristic score"
    assert any("rank=" in o for o in quant_analyst["observations"])
    assert "heuristic_score" not in " ".join(quant_analyst["observations"])


def test_unavailable_analysts_still_report_unavailable(tmp_path):
    """Wiring quant in must not have started fabricating the other roles."""
    loop, store, _broker, session = build_world(tmp_path)
    loop.run_cycle()
    analysts = {a["role"]: a for a in
                events_of(store, session.session_id, "agent")[-1]["payload"]["analysts"]}
    for role in ("fundamental_analyst", "news_analyst", "sentiment_analyst"):
        assert analysts[role]["status"] == "unavailable"
        assert analysts[role]["confidence"] == 0.0


def test_the_loop_runs_three_independent_books_not_broker_mirrors(tmp_path):
    loop, _store, broker, _session = build_world(tmp_path)
    loop.run_cycle()

    books = [loop.shadow.books[a] for a in ARMS]
    assert len({id(b.shares) for b in books}) == 3
    # a shadow book must never be a view of the real broker's positions
    for book in books:
        assert book.shares is not broker.positions
    assert all(len(b.history) == 1 for b in books)


def test_shadow_marks_use_the_same_price_map_for_every_arm(tmp_path):
    loop, _store, _broker, _session = build_world(tmp_path)
    loop.run_cycle()
    rows = [loop.shadow.books[a].history[-1] for a in ARMS]
    assert len({r.cycle for r in rows}) == 1, "arms stepped on different cycles"
    assert len({r.equity_before for r in rows}) == 1, \
        "arms started the cycle from different equity — they were not synchronised"


def test_arm_a_decides_before_deterministic_research_can_veto_it(tmp_path):
    """Arm A is "quant alone decides". If Arm B's research-confidence gate
    could suppress Arm A's decision, A would silently be a subset of B."""
    from noalpha.trading.autonomous import ResearchOutcome

    loop, _store, _session_store, _session = build_world(tmp_path)

    def useless_research(candidate, feed, quant_view=None):
        return ResearchOutcome(thesis="", bull_case=[], bear_case=[], risks=[],
                               invalidation_conditions=[], confidence=0.1,
                               evidence=[])

    loop.engine.research = useless_research
    loop.run_cycle()

    per_arm = loop._arm_decisions
    assert per_arm, "no arm decisions were recorded at all"
    for symbol, arms in per_arm.items():
        assert "quant_only" in arms, f"Arm A never decided on {symbol}"
        assert arms["deterministic_hybrid"].source == "research_confidence_gate"


def test_the_risk_day_state_is_persisted_on_the_session(tmp_path):
    loop, _store, _broker, session = build_world(tmp_path)
    loop.run_cycle()
    assert session.risk_day_state.get("trading_date")
    assert "realized_daily_loss_pct" in session.risk_day_state


def test_system_truth_exposes_selection_shadow_and_risk_state(tmp_path):
    loop, _store, _broker, _session = build_world(tmp_path)
    loop.run_cycle()
    truth = loop.system_truth()

    assert truth["candidate_selection"]["status"] == "QUANT_LED"
    assert truth["shadow"]["status"] == "INSUFFICIENT_SAMPLE"
    assert truth["risk_state"]["trading_date"]


def test_shadow_summary_never_claims_alpha_on_a_short_sample(tmp_path):
    loop, _store, _broker, _session = build_world(tmp_path)
    summary = loop.run_cycle()
    shadow = summary["shadow"]
    assert shadow["sample_sufficiency"] == "INSUFFICIENT_SAMPLE"
    assert loop.shadow.attribution()["verdict"] == "INSUFFICIENT_SAMPLE"


# ══════════════════════════════════════════════════════════════════════
# Part 2 — attacks. Every one of these must FAIL to break the system.
# ══════════════════════════════════════════════════════════════════════


def test_attack_same_cycle_price_move_cannot_be_harvested():
    """Attack: decide at t knowing p[t], and try to book p[t]/p[t−1] − 1."""
    book = ShadowPortfolio("attacker", 1000.0, cost_bps=0.0)
    book.step(1, {"A": 100.0}, {})            # flat, on purpose
    row = book.step(2, {"A": 1000.0}, {"A": 1.0})   # +900% already happened
    assert row.gross_return == pytest.approx(0.0)
    assert book.equity == pytest.approx(1000.0)


def test_attack_repeated_marking_cannot_compound_one_move():
    """Attack: call the mark path many times in a cycle to re-earn a gain."""
    book = ShadowPortfolio("attacker", 1000.0, cost_bps=0.0)
    book.step(1, {"A": 100.0}, {"A": 1.0})
    book.step(2, {"A": 200.0}, {"A": 1.0})
    equity_after_move = book.equity
    for _ in range(10):
        book.mark({"A": 200.0})
    assert book.equity == pytest.approx(equity_after_move), \
        "re-marking the same price compounded the move"


def test_attack_negative_or_zero_prices_cannot_zero_a_book():
    book = ShadowPortfolio("attacker", 1000.0, cost_bps=0.0)
    book.step(1, {"A": 100.0}, {"A": 1.0})
    row = book.step(2, {"A": 0.0}, None)
    assert row.unpriced == ["A"]
    assert book.equity == pytest.approx(1000.0)


def test_attack_an_arm_cannot_be_handed_another_arms_targets(tmp_path):
    """Attack: reintroduce `llm_hybrid = deterministic_hybrid`. Even given the
    SAME target dict object, the books must stay separate."""
    loop, _store, _broker, _session = build_world(tmp_path)
    shared = {"SYM1": 0.1}
    loop.shadow.step(1, {"SYM1": 100.0}, {a: shared for a in ARMS})
    loop.shadow.step(2, {"SYM1": 150.0}, {a: shared for a in ARMS})
    books = [loop.shadow.books[a] for a in ARMS]
    assert len({id(b.shares) for b in books}) == 3
    # identical inputs SHOULD give identical results — that is fairness, not aliasing
    assert len({b.equity for b in books}) == 1
    books[0].shares["SYM1"] = 0.0
    assert books[1].shares["SYM1"] > 0, "mutating one arm changed another"


def test_attack_llm_cannot_place_an_order_without_passing_hard_risk(tmp_path):
    """Attack: an LLM-driven fusion BUY must still be judged by the external
    risk engine. Force the engine to reject and assert nothing fills."""
    loop, store, broker, session = build_world(tmp_path)

    from noalpha.trading.risk import RiskResult
    calls = {"n": 0}

    def always_reject(*args, **kwargs):
        calls["n"] += 1
        return RiskResult(approved=False, rejected=True,
                          violations=["attack: forced rejection"],
                          reason="forced rejection")

    loop.risk.check = always_reject
    summary = loop.run_cycle()

    assert calls["n"] > 0, "the risk engine was never consulted"
    assert not summary["actions"], "an order filled without risk approval"
    assert not broker.fills


def test_attack_stale_quotes_cannot_trade(tmp_path):
    loop, _store, broker, _session = build_world(tmp_path)
    loop._tradable = lambda quote: (False, "stale")
    summary = loop.run_cycle()
    assert not summary["actions"]
    assert not broker.fills


def test_attack_kill_switch_cannot_be_bypassed(tmp_path):
    loop, _store, broker, _session = build_world(tmp_path)
    broker.activate_kill_switch("attack test")
    summary = loop.run_cycle()
    assert not summary["actions"]
    assert not broker.fills


def test_attack_a_restart_cannot_clear_the_daily_loss_limit(tmp_path):
    """Attack: crash-loop so the risk engine re-anchors start-of-day equity and
    realized PnL at the CURRENT (already-lost) level, clearing the limit."""
    from noalpha.trading.autonomous import AutonomousTradingLoop
    from noalpha.trading.domain import Position

    loop, store, broker, session = build_world(tmp_path)
    loop.run_cycle()                       # anchors the day at ~zero realized PnL
    trading_date = loop.risk.day_state.trading_date
    assert trading_date

    # Book a real −50% realized loss on the ledger the risk engine reads.
    broker.positions["LOSER"] = Position(symbol="LOSER", quantity=0.0,
                                         realized_pnl=-500_000.0)
    proposal = TradeProposal(symbol="SYM1", action=TradeAction.BUY, target_weight=0.05)
    blocked = loop.risk.check(proposal, broker.account, broker.positions, [], 100.0,
                              trading_date=trading_date)
    assert blocked.rejected, "the loss was not caught in the first place"
    session.risk_day_state = loop.risk.day_state.to_dict()

    # …now crash and come back up.
    restarted = AutonomousTradingLoop(session, store, broker, feed=loop.feed,
                                      universe_provider=lambda: [])
    still_blocked = restarted.risk.check(proposal, broker.account, broker.positions,
                                         [], 100.0, trading_date=trading_date)
    assert still_blocked.rejected, "restarting cleared today's realized-loss limit"


def test_attack_a_heuristic_gate_cannot_be_slipped_in_front_of_quant(tmp_path):
    """Attack: the loop must not consult `buy_score_threshold` for admission
    while an ACTIVE model is ranking. Asserted by driving every heuristic
    score to zero and checking research still happens."""
    loop, store, _broker, session = build_world(tmp_path)
    original = loop.engine.scan_universe

    def zeroed(universe, feed):
        candidates, skipped = original(universe, feed)
        for c in candidates:
            c.score = 0.0
        return candidates, skipped

    loop.engine.scan_universe = zeroed
    loop.run_cycle()
    assert events_of(store, session.session_id, "candidate"), \
        "a heuristic score of zero blocked an ACTIVE quant model's candidates"


def test_attack_a_tampered_artifact_degrades_the_loop_instead_of_running(tmp_path):
    """Attack: swap the model file under an ACTIVE registration. The loop must
    report quant UNAVAILABLE, not quietly run something else and keep the
    quant-branded provenance on its decisions."""
    loop, store, _broker, session = build_world(tmp_path)
    artifact = next((tmp_path / "models").glob("mdl-*.pkl"))
    artifact.write_bytes(artifact.read_bytes() + b"tampered")

    loop.run_cycle()
    truth = loop.system_truth()
    assert truth["components"]["quant"]["status"] == "UNAVAILABLE"
    assert truth["candidate_selection"]["status"] == "HEURISTIC_FALLBACK"
    for cand in events_of(store, session.session_id, "candidate"):
        assert cand["payload"]["candidate_source"] == HEURISTIC_FALLBACK, \
            "a heuristic-selected candidate was still labelled quant-driven"


def test_attack_shadow_cannot_reach_sample_sufficiency_on_empty_cycles(tmp_path):
    """Attack: spin the loop on an empty universe to inflate the cycle count
    until the report calls the sample sufficient."""
    loop, _store, _broker, _session = build_world(tmp_path)
    loop.universe_provider = lambda: []
    for _ in range(40):
        loop.run_cycle()
    sample = loop.shadow.sample_state()
    assert sample["verdict"] == "INSUFFICIENT_SAMPLE"
    assert sample["cycles"] == 0, "empty cycles were counted as observations"


def test_attack_decision_changes_alone_do_not_produce_an_alpha_claim(tmp_path):
    """Attack: log many `llm_changed_decision=True` deltas and hope the report
    reads that as the LLM adding alpha."""
    from noalpha.trading.shadow import DecisionDelta

    loop, _store, _broker, _session = build_world(tmp_path)
    for i in range(200):
        loop.shadow.record_delta(DecisionDelta(cycle=i, symbol="X",
                                               llm_changed_decision=True,
                                               change_type="buy->no_action"))
    report = loop.shadow.attribution()
    assert report["verdict"] == "INSUFFICIENT_SAMPLE"
    assert report["decision_attribution"]["decision_change_count"] == 200
    # decision change count is NOT return: the portfolio side must stay flat
    assert report["portfolio_attribution"]["llm_vs_quant"]["return_delta"] == \
        pytest.approx(0.0, abs=1e-9)
