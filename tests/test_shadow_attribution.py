"""Shadow attribution invariants.

These tests exist because every one of them corresponds to a real bug that
was shipped and measured with:

  * `arm_targets["llm_hybrid"] = arm_targets["deterministic_hybrid"]`
    — arms B and C were literally the same object
  * both of those targets were copied from the REAL broker's positions
    — the "counterfactual" books were mirrors, not counterfactuals
  * `day_returns[sym] = market_price / average_cost - 1`
    — cumulative-since-entry return, re-compounded every single cycle

If any of these regress, the answer to "does the LLM add incremental alpha?"
becomes unfalsifiable, so they are asserted at the invariant level rather than
through the numbers of any particular run.
"""
from __future__ import annotations

import math

import pytest

from noalpha.trading.shadow import (
    ARMS, MIN_CYCLES_FOR_ATTRIBUTION, MIN_DECISION_CHANGES_FOR_ATTRIBUTION,
    ArmDecision, DecisionDelta, ShadowArena, ShadowPortfolio,
    ShadowPortfolioPolicy, classify_change, compute_arm_metrics,
)


# ── period return correctness ──


def test_period_return_uses_consecutive_marks_not_average_cost():
    """r[t] = p[t]/p[t-1] − 1, applied ONCE. The old code used
    price/average_cost, which re-earns the entire since-entry gain every
    cycle: a stock bought at 100 and sitting at 110 for ten cycles compounded
    +10% ten times into +159%."""
    book = ShadowPortfolio("test", 1000.0, cost_bps=0.0)

    # cycle 1: buy the whole book at 100
    book.step(1, {"A": 100.0}, {"A": 1.0})
    assert book.equity == pytest.approx(1000.0)

    # cycle 2: price moves to 110 → +10%, once
    row2 = book.step(2, {"A": 110.0}, {"A": 1.0})
    assert row2.net_return == pytest.approx(0.10)
    assert book.equity == pytest.approx(1100.0)

    # cycles 3..6: price is FLAT at 110 → 0% each, not +10% each
    for cycle in range(3, 7):
        row = book.step(cycle, {"A": 110.0}, {"A": 1.0})
        assert row.net_return == pytest.approx(0.0, abs=1e-9), (
            "a flat price produced a non-zero return — the since-entry gain is "
            "being re-compounded")
    assert book.equity == pytest.approx(1100.0)


def test_portfolio_return_is_previous_weights_times_this_cycle_returns():
    """portfolio_return[t] = Σ w[t−1] · r[t]. The weights that earn a cycle's
    return are LAST cycle's, never this cycle's."""
    book = ShadowPortfolio("test", 1000.0, cost_bps=0.0)
    book.step(1, {"A": 100.0, "B": 50.0}, {"A": 0.5, "B": 0.5})

    # A +20%, B −10% → 0.5*0.20 + 0.5*(−0.10) = +5%
    row = book.step(2, {"A": 120.0, "B": 45.0}, {"A": 0.5, "B": 0.5})
    assert row.gross_return == pytest.approx(0.05)


def test_mark_happens_before_rebalance_so_a_new_buy_earns_nothing_this_cycle():
    """The look-ahead test. An arm that decides to buy at cycle t must not
    collect the move that carried the price INTO t."""
    book = ShadowPortfolio("test", 1000.0, cost_bps=0.0)
    # cycle 1: hold nothing at all
    row1 = book.step(1, {"A": 100.0}, {})
    assert row1.net_return == pytest.approx(0.0)

    # cycle 2: the price already doubled BEFORE we decide to buy.
    # Buying now must yield 0% this cycle, not +100%.
    row2 = book.step(2, {"A": 200.0}, {"A": 1.0})
    assert row2.gross_return == pytest.approx(0.0), (
        "a same-cycle buy collected the same-cycle move — look-ahead")
    assert book.equity == pytest.approx(1000.0)

    # the gain only arrives on the NEXT price move
    row3 = book.step(3, {"A": 220.0}, {"A": 1.0})
    assert row3.gross_return == pytest.approx(0.10)


def test_transaction_cost_is_charged_on_traded_notional():
    book = ShadowPortfolio("test", 1000.0, cost_bps=100.0)   # 1% one way
    row = book.step(1, {"A": 100.0}, {"A": 1.0})
    # Cost-aware sizing: a fully-invested target buys 990, not 1000, so the
    # 9.9 of cost leaves the book at its target weight instead of levered.
    assert row.turnover == pytest.approx(0.99)
    assert row.cost_paid == pytest.approx(9.9)
    assert book.equity == pytest.approx(990.1)
    assert row.net_return == pytest.approx(-0.0099)

    # an unchanged decision must never generate another fee
    row2 = book.step(2, {"A": 100.0}, {"A": 1.0})
    assert row2.cost_paid == pytest.approx(0.0), (
        "an unchanged target still traded — cost-induced churn")
    assert row2.trades == 0


def test_unpriced_holding_is_reported_not_silently_zeroed():
    book = ShadowPortfolio("test", 1000.0, cost_bps=0.0)
    book.step(1, {"A": 100.0}, {"A": 1.0})
    row = book.step(2, {}, None)
    assert row.unpriced == ["A"]
    assert book.equity == pytest.approx(1000.0)


# ── arm independence ──


def _arena(tmp_path) -> ShadowArena:
    return ShadowArena("sess-test", 10_000.0, root=tmp_path,
                       policy=ShadowPortfolioPolicy(base_weight=0.10, max_weight=0.20))


def test_the_three_books_are_distinct_objects(tmp_path):
    arena = _arena(tmp_path)
    books = [arena.books[a] for a in ARMS]
    assert len({id(b) for b in books}) == 3
    assert len({id(b.shares) for b in books}) == 3
    assert len({id(b.marks) for b in books}) == 3
    assert len({id(b.history) for b in books}) == 3


def test_different_decisions_produce_different_positions_cash_and_equity(tmp_path):
    """B and C decide differently → their books must diverge in every
    dimension the report claims they own."""
    arena = _arena(tmp_path)
    prices = {"A": 100.0, "B": 100.0}

    targets = {
        "quant_only": arena.targets_for("quant_only", [
            ArmDecision(arm="quant_only", symbol="A", action="buy")]),
        "deterministic_hybrid": arena.targets_for("deterministic_hybrid", [
            ArmDecision(arm="deterministic_hybrid", symbol="A", action="buy"),
            ArmDecision(arm="deterministic_hybrid", symbol="B", action="buy")]),
        # Arm C's LLM vetoed B and only took A
        "llm_hybrid": arena.targets_for("llm_hybrid", [
            ArmDecision(arm="llm_hybrid", symbol="A", action="buy"),
            ArmDecision(arm="llm_hybrid", symbol="B", action="no_action")]),
    }
    assert targets["deterministic_hybrid"] != targets["llm_hybrid"], (
        "a decision difference did not translate into a target difference")
    arena.step(1, prices, targets)

    b, c = arena.books["deterministic_hybrid"], arena.books["llm_hybrid"]
    assert set(b.shares) == {"A", "B"}
    assert set(c.shares) == {"A"}
    assert b.cash != c.cash
    assert b.turnover_total > c.turnover_total
    assert b.fees_paid > c.fees_paid

    # B holds the loser, C vetoed it → equities must diverge
    arena.step(2, {"A": 100.0, "B": 50.0}, targets)
    assert arena.books["deterministic_hybrid"].equity < arena.books["llm_hybrid"].equity


def test_all_arms_are_marked_on_the_same_price_map_and_cycle(tmp_path):
    arena = _arena(tmp_path)
    targets = {arm: arena.targets_for(arm, [ArmDecision(arm=arm, symbol="A", action="buy")])
               for arm in ARMS}
    arena.step(1, {"A": 100.0}, targets)
    row = arena.step(2, {"A": 110.0}, targets)
    # identical decisions + identical prices + identical costs ⇒ identical books
    equities = {arena.books[a].equity for a in ARMS}
    assert len(equities) == 1, "arms with identical decisions diverged — unfair comparison"
    assert row["priced_symbols"] == ["A"]
    for arm in ARMS:
        assert row["arms"][arm]["cycle_row"]["cycle"] == 2


def test_no_arm_reads_the_real_broker(tmp_path):
    """The books must be driven purely by ArmDecisions. Guarded structurally:
    a ShadowArena has no reference to a broker, account or position store."""
    arena = _arena(tmp_path)
    forbidden = {"broker", "account", "positions", "real_book"}
    assert not (forbidden & set(vars(arena)))
    for book in arena.books.values():
        assert not (forbidden & set(vars(book)))


# ── sizing policy ──


def test_no_action_does_not_liquidate_an_existing_holding():
    policy = ShadowPortfolioPolicy(base_weight=0.1, max_weight=0.2)
    out = policy.targets({"A": 0.1}, [ArmDecision(arm="x", symbol="A", action="no_action")])
    assert out["A"] == pytest.approx(0.1)


def test_reduce_halves_and_sell_closes():
    policy = ShadowPortfolioPolicy(base_weight=0.1, max_weight=0.2)
    assert policy.targets({"A": 0.1}, [ArmDecision(arm="x", symbol="A", action="reduce")])["A"] \
        == pytest.approx(0.05)
    assert "A" not in policy.targets(
        {"A": 0.1}, [ArmDecision(arm="x", symbol="A", action="sell")])


def test_gross_exposure_is_capped():
    policy = ShadowPortfolioPolicy(base_weight=0.5, max_weight=0.5, max_gross=1.0)
    out = policy.targets({}, [ArmDecision(arm="x", symbol=s, action="buy")
                              for s in "ABCDEF"])
    assert sum(out.values()) == pytest.approx(1.0)


# ── attribution ──


def test_attribution_reports_insufficient_sample_until_both_thresholds_are_met(tmp_path):
    arena = _arena(tmp_path)
    targets = {arm: {"A": 0.1} for arm in ARMS}
    for cycle in range(MIN_CYCLES_FOR_ATTRIBUTION + 2):
        arena.step(cycle, {"A": 100.0 + cycle}, targets)
    # cycles are sufficient, decision changes are not
    assert arena.attribution()["verdict"] == "INSUFFICIENT_SAMPLE"

    for i in range(MIN_DECISION_CHANGES_FOR_ATTRIBUTION):
        arena.record_delta(DecisionDelta(
            cycle=i, symbol=f"S{i}", llm_changed_decision=True,
            change_type=classify_change("buy", "no_action")))
    assert arena.attribution()["verdict"] == "SAMPLE_OK"


def test_insufficient_sample_when_cycles_are_short(tmp_path):
    arena = _arena(tmp_path)
    for i in range(MIN_DECISION_CHANGES_FOR_ATTRIBUTION + 5):
        arena.record_delta(DecisionDelta(cycle=i, symbol=f"S{i}",
                                         llm_changed_decision=True,
                                         change_type="buy->no_action"))
    arena.step(1, {"A": 100.0}, {arm: {} for arm in ARMS})
    report = arena.attribution()
    assert report["verdict"] == "INSUFFICIENT_SAMPLE"
    assert report["sample"]["cycles"] < MIN_CYCLES_FOR_ATTRIBUTION
    # cost-per-return metrics must not be published on an insufficient sample
    assert report["llm_cost"]["tokens_per_return_point"] is None


def test_decision_attribution_classifies_change_types(tmp_path):
    arena = _arena(tmp_path)
    for change in ["buy->no_action", "buy->no_action", "buy->reduce", "hold->sell"]:
        arena.record_delta(DecisionDelta(symbol="X", llm_changed_decision=True,
                                         change_type=change))
    arena.record_delta(DecisionDelta(symbol="X", llm_changed_decision=False))
    d = arena.decision_attribution()
    assert d["decision_change_count"] == 4
    assert d["decisions_total"] == 5
    assert d["by_change_type"]["buy->no_action"] == 2
    assert d["llm_veto_count"] == 2          # → no_action
    assert d["llm_reduce_count"] == 2        # → reduce / sell
    assert d["llm_confirmation_count"] == 1
    assert d["decision_change_rate"] == pytest.approx(0.8)


def test_portfolio_attribution_deltas_come_from_the_real_books(tmp_path):
    arena = _arena(tmp_path)
    # C buys the winner, A and B stay flat
    arena.step(1, {"W": 100.0}, {
        "quant_only": {}, "deterministic_hybrid": {},
        "llm_hybrid": arena.targets_for("llm_hybrid", [
            ArmDecision(arm="llm_hybrid", symbol="W", action="buy")])})
    arena.step(2, {"W": 200.0}, {
        "quant_only": {}, "deterministic_hybrid": {},
        "llm_hybrid": {"W": arena.books["llm_hybrid"].weights().get("W", 0.0)}})
    pa = arena.portfolio_attribution()
    assert pa["llm_vs_quant"]["return_delta"] > 0
    assert pa["llm_vs_deterministic"]["return_delta"] > 0
    assert pa["llm_vs_quant"]["turnover_delta"] > 0


def test_metrics_do_not_annualise_a_scan_cycle(tmp_path):
    """Sharpe is per-cycle. Annualising a scan cycle would fabricate a
    calendar the system does not have."""
    arena = _arena(tmp_path)
    for cycle in range(1, 6):
        arena.step(cycle, {"A": 100.0 + cycle}, {arm: {"A": 0.5} for arm in ARMS})
    m = compute_arm_metrics(arena.books["quant_only"])
    assert m.sharpe_per_cycle is not None
    assert not hasattr(m, "annualized_return")
    assert not hasattr(m, "sharpe")


def test_max_drawdown_is_measured_against_the_running_peak(tmp_path):
    arena = _arena(tmp_path)
    targets = {arm: {"A": 1.0} for arm in ARMS}
    arena.step(1, {"A": 100.0}, targets)
    arena.step(2, {"A": 200.0}, targets)     # peak
    arena.step(3, {"A": 100.0}, targets)     # −50% from peak
    m = compute_arm_metrics(arena.books["quant_only"])
    assert m.max_drawdown == pytest.approx(-0.5, abs=0.02)


def test_books_survive_a_restart(tmp_path):
    """A forward experiment measured over weeks must not silently reset its
    books every time the process restarts while the decision log keeps
    growing — that combination fabricates flat arms."""
    arena = _arena(tmp_path)
    arena.step(1, {"A": 100.0}, {arm: {"A": 0.5} for arm in ARMS})
    arena.step(2, {"A": 150.0}, {arm: {"A": 0.5} for arm in ARMS})
    before = {arm: arena.books[arm].equity for arm in ARMS}

    restored = ShadowArena("sess-test", 10_000.0, root=tmp_path)
    assert {arm: restored.books[arm].equity for arm in ARMS} == before
    assert len(restored.books["llm_hybrid"].history) == 2
