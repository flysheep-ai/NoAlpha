"""Forward shadow comparison of the three experiment arms.

Arm A quant_only            — the quant signal alone decides
Arm B deterministic_hybrid  — quant + deterministic research ensemble
Arm C llm_hybrid            — quant + REAL LLM research fusion

Only one arm drives the real VirtualBroker (session.research_mode decides);
all three ALSO run here as fully independent counterfactual books. Each arm
owns its own cash, shares, marks, turnover, cost and equity history — no arm
reads another arm's state and no arm reads the real broker's positions.

Fairness contract (every one of these is asserted in tests):

  * same universe   — the arms see the same candidate set each cycle
  * same prices     — ONE price map per cycle is handed to every book
  * same cycle      — one `step()` advances all three at once
  * same cost model — identical `cost_bps`, charged on traded notional
  * same sizing     — one ShadowPortfolioPolicy converts decisions to weights
  * only the DECISION differs

Return accounting (第四十六条). Per cycle, in this order:

  1. mark  — this cycle's prices are applied to the holdings decided LAST
             cycle:  r[t] = Σ w[t-1] · (p[t]/p[t-1] − 1)
  2. trade — the new targets are executed at those same prices and the
             transaction cost is charged:  net[t] = r[t] − cost[t]

Marking before rebalancing is what makes look-ahead structurally impossible:
a decision made at t can never collect the return that carried the book INTO
t. There is no `price / average_cost` anywhere in this module — that quantity
is cumulative-since-entry and re-compounding it every cycle was the bug this
module was rewritten to kill.

Stated limits, not hidden: these books ignore lot sizes, partial fills and
intraday microstructure. They measure signal and decision quality, not
execution quality.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ARMS = ("quant_only", "deterministic_hybrid", "llm_hybrid")

# One-way transaction cost on traded notional (15 bps ≈ 30 bps round trip,
# the assumption the pre-rewrite book used).
DEFAULT_COST_BPS = 15.0

# A forward experiment may not be turned into a statistical claim before it
# has enough independent observations. Below either threshold every verdict
# reads INSUFFICIENT_SAMPLE, however pretty the equity delta looks.
MIN_CYCLES_FOR_ATTRIBUTION = 20
MIN_DECISION_CHANGES_FOR_ATTRIBUTION = 10

# Decision vocabulary shared by all three arms.
BUY, HOLD, REDUCE, SELL, NO_ACTION, WATCH = (
    "buy", "hold", "reduce", "sell", "no_action", "watch")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── decisions ──


@dataclass
class ArmDecision:
    """What ONE arm would do with ONE instrument in ONE cycle.

    This is the only thing allowed to differ between arms. Everything
    downstream (sizing, costs, marking) is shared machinery.
    """
    arm: str
    symbol: str
    action: str = NO_ACTION
    size_multiplier: float = 1.0        # scales the base target weight
    source: str = ""                    # which policy produced it
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionDelta:
    """Same instrument, same cycle — what each arm decided (第四十九条).

    Decision attribution only. It records that the LLM *changed* a decision;
    it says nothing about whether that change made money. That question is
    answered by portfolio attribution, from the three independent books.
    """
    delta_id: str = field(default_factory=lambda: f"dd-{uuid.uuid4().hex[:10]}")
    as_of: str = field(default_factory=_now)
    cycle: int = 0
    symbol: str = ""
    arm_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    llm_changed_decision: bool = False
    change_type: str = ""               # e.g. "buy->no_action"
    change_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_change(baseline_action: str, llm_action: str) -> str:
    """Canonical `from->to` label for the decision-change histogram."""
    return f"{baseline_action or NO_ACTION}->{llm_action or NO_ACTION}"


# ── the counterfactual book ──


@dataclass
class ShadowCycleRow:
    cycle: int
    as_of: str
    equity_before: float
    equity_after_mark: float
    equity_after_trade: float
    gross_return: float                 # from marking last cycle's holdings
    cost_return: float                  # negative; transaction cost drag
    net_return: float                   # gross + cost
    turnover: float                     # traded notional / equity
    cost_paid: float
    trades: int
    weights: dict[str, float] = field(default_factory=dict)
    unpriced: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ShadowPortfolio:
    """An independent counterfactual book: cash + shares, marked by real prices.

    Holdings are shares, not weights, so equity, turnover and cost are exact
    and never need the drift-renormalisation that hid errors in the previous
    weight-only implementation.
    """

    # Minimum trade size as a fraction of equity. Shared by every arm.
    NO_TRADE_BAND = 0.0005      # 5 bps

    def __init__(self, arm: str, initial_capital: float,
                 cost_bps: float = DEFAULT_COST_BPS) -> None:
        self.arm = arm
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.shares: dict[str, float] = {}
        self.marks: dict[str, float] = {}     # last price each holding was valued at
        self.cost_bps = float(cost_bps)
        self.trade_count = 0
        self.fees_paid = 0.0
        self.turnover_total = 0.0
        self.history: list[ShadowCycleRow] = []

    # ── valuation ──

    @property
    def equity(self) -> float:
        return self.cash + sum(q * self.marks.get(s, 0.0) for s, q in self.shares.items())

    def weights(self) -> dict[str, float]:
        eq = self.equity
        if eq <= 0:
            return {}
        return {s: (q * self.marks.get(s, 0.0)) / eq
                for s, q in self.shares.items() if q > 0}

    # ── the two-phase cycle ──

    def mark(self, prices: dict[str, float]) -> tuple[float, float, list[str]]:
        """Phase 1. Re-value LAST cycle's holdings at THIS cycle's prices.

        Returns (equity_before, equity_after, unpriced_symbols). A holding with
        no price this cycle keeps its previous mark and is reported as unpriced
        rather than silently valued at zero.
        """
        equity_before = self.equity
        unpriced: list[str] = []
        for symbol, qty in self.shares.items():
            if qty <= 0:
                continue
            price = prices.get(symbol)
            if price is None or price <= 0:
                unpriced.append(symbol)
                continue
            self.marks[symbol] = float(price)
        return equity_before, self.equity, unpriced

    def rebalance(self, targets: dict[str, float],
                  prices: dict[str, float]) -> tuple[float, float, int]:
        """Phase 2. Trade toward `targets` (weights) at THIS cycle's prices.

        Returns (turnover, cost_paid, trades). Turnover is traded notional over
        pre-trade equity, counting both buys and sells.

        Sizing is cost-aware in two passes. Sizing against gross equity and
        then paying the cost out of cash leaves the book levered by exactly the
        cost, which the next cycle unwinds — a churn loop that charged fees
        forever on a book whose decisions never changed. The second pass sizes
        against equity net of the first pass's estimated cost.
        """
        equity = self.equity
        if equity <= 0:
            return 0.0, 0.0, 0

        cost_rate = self.cost_bps / 10_000.0
        symbols = sorted(set(targets) | set(self.shares))

        def plan(budget: float) -> list[tuple[str, float, float, float]]:
            """(symbol, price, delta_qty, notional) for a given equity budget."""
            out = []
            for symbol in symbols:
                price = prices.get(symbol) or self.marks.get(symbol) or 0.0
                if price <= 0:
                    # cannot trade what cannot be priced; the holding stays
                    continue
                target_w = max(0.0, float(targets.get(symbol, 0.0)))
                delta_qty = (target_w * budget) / price - self.shares.get(symbol, 0.0)
                notional = abs(delta_qty) * price
                # No-trade band: identical for every arm, so it cannot bias the
                # comparison. Without it, floating-point drift alone trades.
                if notional < equity * self.NO_TRADE_BAND:
                    continue
                out.append((symbol, price, delta_qty, notional))
            return out

        first = plan(equity)
        estimated_cost = sum(n for *_, n in first) * cost_rate
        planned = plan(equity - estimated_cost) if estimated_cost > 0 else first

        traded_notional = 0.0
        trades = 0
        for symbol, price, delta_qty, notional in planned:
            self.cash -= delta_qty * price
            self.shares[symbol] = self.shares.get(symbol, 0.0) + delta_qty
            self.marks[symbol] = price
            traded_notional += notional
            trades += 1

        cost = traded_notional * cost_rate
        self.cash -= cost
        self.fees_paid += cost
        self.trade_count += trades
        turnover = traded_notional / equity if equity > 0 else 0.0
        self.turnover_total += turnover
        # drop dust so the book does not accumulate zero rows forever
        self.shares = {s: q for s, q in self.shares.items()
                       if q * self.marks.get(s, 0.0) > equity * 1e-6}
        return turnover, cost, trades

    def step(self, cycle: int, prices: dict[str, float],
             targets: dict[str, float] | None) -> ShadowCycleRow:
        """One full cycle: mark last cycle's book, THEN trade. Never reorder."""
        equity_before, equity_after_mark, unpriced = self.mark(prices)
        turnover, cost, trades = (0.0, 0.0, 0)
        if targets is not None:
            turnover, cost, trades = self.rebalance(targets, prices)
        equity_after_trade = self.equity

        gross = (equity_after_mark / equity_before - 1.0) if equity_before > 0 else 0.0
        cost_ret = (-cost / equity_after_mark) if equity_after_mark > 0 else 0.0
        net = (equity_after_trade / equity_before - 1.0) if equity_before > 0 else 0.0

        row = ShadowCycleRow(
            cycle=cycle, as_of=_now(),
            equity_before=round(equity_before, 6),
            equity_after_mark=round(equity_after_mark, 6),
            equity_after_trade=round(equity_after_trade, 6),
            gross_return=gross, cost_return=cost_ret, net_return=net,
            turnover=turnover, cost_paid=cost, trades=trades,
            weights={s: round(w, 6) for s, w in self.weights().items()},
            unpriced=unpriced,
        )
        self.history.append(row)
        return row

    # ── persistence (a forward experiment must survive a restart) ──

    def to_state(self) -> dict[str, Any]:
        return {"arm": self.arm, "initial_capital": self.initial_capital,
                "cash": self.cash, "shares": dict(self.shares), "marks": dict(self.marks),
                "cost_bps": self.cost_bps, "trade_count": self.trade_count,
                "fees_paid": self.fees_paid, "turnover_total": self.turnover_total,
                "history": [r.to_dict() for r in self.history]}

    def load_state(self, state: dict[str, Any]) -> None:
        self.cash = float(state.get("cash", self.initial_capital))
        self.shares = {k: float(v) for k, v in (state.get("shares") or {}).items()}
        self.marks = {k: float(v) for k, v in (state.get("marks") or {}).items()}
        self.trade_count = int(state.get("trade_count", 0))
        self.fees_paid = float(state.get("fees_paid", 0.0))
        self.turnover_total = float(state.get("turnover_total", 0.0))
        self.history = [ShadowCycleRow(**r) for r in (state.get("history") or [])]

    def to_dict(self) -> dict[str, Any]:
        equity = self.equity
        return {
            "arm": self.arm,
            "equity": round(equity, 2),
            "cash": round(self.cash, 2),
            "return_pct": round((equity / self.initial_capital - 1) * 100, 4)
            if self.initial_capital else 0.0,
            "weights": {k: round(v, 4) for k, v in self.weights().items()},
            "positions": {k: round(v, 4) for k, v in self.shares.items() if v > 0},
            "trade_count": self.trade_count,
            "fees_paid": round(self.fees_paid, 2),
            "turnover_total": round(self.turnover_total, 4),
            "cycles": len(self.history),
        }


# ── shared sizing: identical for every arm, so only the decision differs ──


class ShadowPortfolioPolicy:
    """Deterministic decision → target-weight map, shared by all three arms.

    Deliberately dumb and identical across arms. If Arm C sized differently
    from Arm B for reasons other than its decisions, the comparison would
    measure the sizing rule instead of the LLM.
    """

    def __init__(self, base_weight: float = 0.06, max_weight: float = 0.10,
                 max_gross: float = 1.0) -> None:
        self.base_weight = base_weight
        self.max_weight = max_weight
        self.max_gross = max_gross

    def targets(self, current_weights: dict[str, float],
                decisions: list[ArmDecision]) -> dict[str, float]:
        out = {s: w for s, w in current_weights.items() if w > 0}
        for d in decisions:
            held = out.get(d.symbol, 0.0)
            if d.action == BUY:
                want = min(self.base_weight * max(d.size_multiplier, 0.0), self.max_weight)
                out[d.symbol] = max(held, want)
            elif d.action == REDUCE:
                out[d.symbol] = held * 0.5
            elif d.action == SELL:
                out[d.symbol] = 0.0
            # hold / no_action / watch leave the existing weight untouched:
            # "no_action" means "do not open", not "liquidate".
        out = {s: w for s, w in out.items() if w > 1e-6}
        gross = sum(out.values())
        if gross > self.max_gross and gross > 0:
            scale = self.max_gross / gross
            out = {s: w * scale for s, w in out.items()}
        return out


# ── metrics ──


@dataclass
class ArmMetrics:
    arm: str
    cycles: int = 0
    final_equity: float = 0.0
    cumulative_return: float = 0.0
    mean_cycle_return: float = 0.0
    cycle_return_vol: float = 0.0
    sharpe_per_cycle: float | None = None
    max_drawdown: float = 0.0
    total_turnover: float = 0.0
    total_cost: float = 0.0
    cost_drag: float = 0.0              # total cost / initial capital
    trades: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_arm_metrics(book: ShadowPortfolio) -> ArmMetrics:
    rows = book.history
    rets = [r.net_return for r in rows]
    n = len(rets)
    mean = sum(rets) / n if n else 0.0
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    vol = math.sqrt(var)
    # Sharpe is reported PER CYCLE and never annualised: a scan cycle is not a
    # calendar period, so any annualisation factor here would be fabricated.
    sharpe = (mean / vol) if vol > 1e-12 and n > 1 else None

    peak = book.initial_capital
    max_dd = 0.0
    for r in rows:
        peak = max(peak, r.equity_after_trade)
        if peak > 0:
            max_dd = min(max_dd, r.equity_after_trade / peak - 1.0)

    return ArmMetrics(
        arm=book.arm, cycles=n, final_equity=book.equity,
        cumulative_return=(book.equity / book.initial_capital - 1.0)
        if book.initial_capital else 0.0,
        mean_cycle_return=mean, cycle_return_vol=vol, sharpe_per_cycle=sharpe,
        max_drawdown=max_dd, total_turnover=book.turnover_total,
        total_cost=book.fees_paid,
        cost_drag=(book.fees_paid / book.initial_capital) if book.initial_capital else 0.0,
        trades=book.trade_count,
    )


# ── the arena ──


class ShadowArena:
    """Holds the three independent books + append-only decision/equity logs."""

    def __init__(self, session_id: str, initial_capital: float,
                 root: str | Path = ".cache/quant/shadow",
                 cost_bps: float = DEFAULT_COST_BPS,
                 policy: ShadowPortfolioPolicy | None = None) -> None:
        self.session_id = session_id
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.initial_capital = float(initial_capital)
        self.books = {arm: ShadowPortfolio(arm, initial_capital, cost_bps) for arm in ARMS}
        self.policy = policy or ShadowPortfolioPolicy()
        self.deltas_path = self.root / f"{session_id}_deltas.jsonl"
        self.equity_path = self.root / f"{session_id}_equity.jsonl"
        self.state_path = self.root / f"{session_id}_books.json"
        self.llm_stats = {"calls": 0, "tokens": 0, "latency_ms_total": 0.0,
                          "failures": 0, "fallback_cycles": 0, "vetoes": 0,
                          "confirmations": 0, "reduces_triggered": 0}
        self._load_state()

    # ── decision log ──

    def record_delta(self, delta: DecisionDelta) -> None:
        with self.deltas_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(delta.to_dict(), ensure_ascii=False) + "\n")

    def load_deltas(self, limit: int = 10_000) -> list[dict[str, Any]]:
        if not self.deltas_path.exists():
            return []
        lines = self.deltas_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]

    # ── the cycle ──

    def targets_for(self, arm: str, decisions: list[ArmDecision]) -> dict[str, float]:
        """Each arm's own target weights, derived from its OWN book state and
        its OWN decisions through the shared sizing policy."""
        book = self.books[arm]
        return self.policy.targets(book.weights(), decisions)

    def step(self, cycle: int, prices: dict[str, float],
             arm_targets: dict[str, dict[str, float]]) -> dict[str, Any]:
        """Advance ALL arms one cycle against the SAME price map.

        `prices` is a single shared dict on purpose: it is structurally
        impossible for two arms to be marked on different quotes.
        """
        rows = {arm: self.books[arm].step(cycle, prices, arm_targets.get(arm))
                for arm in ARMS}
        row = {"as_of": _now(), "cycle": cycle,
               "priced_symbols": sorted(prices.keys()),
               "arms": {arm: {**self.books[arm].to_dict(), "cycle_row": rows[arm].to_dict()}
                        for arm in ARMS},
               "llm_stats": dict(self.llm_stats)}
        with self.equity_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._save_state()
        return row

    # ── persistence ──

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(json.dumps(
                {"session_id": self.session_id,
                 "books": {a: b.to_state() for a, b in self.books.items()},
                 "llm_stats": self.llm_stats},
                ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for arm, book_state in (state.get("books") or {}).items():
            if arm in self.books:
                self.books[arm].load_state(book_state)
        self.llm_stats.update(state.get("llm_stats") or {})

    # ── attribution ──

    def decision_attribution(self) -> dict[str, Any]:
        """What the LLM CHANGED. Says nothing about whether it paid off."""
        deltas = self.load_deltas()
        changed = [d for d in deltas if d.get("llm_changed_decision")]
        by_type: dict[str, int] = {}
        for d in changed:
            key = d.get("change_type") or "unknown"
            by_type[key] = by_type.get(key, 0) + 1
        vetoes = sum(n for k, n in by_type.items() if k.endswith(("->no_action", "->watch")))
        reduces = sum(n for k, n in by_type.items() if k.endswith(("->reduce", "->sell")))
        # Equal decisions are an AI confirmation only when usable AI research
        # actually participated. Missing/legacy provenance cannot prove that.
        confirmations = sum(
            not d.get("llm_changed_decision")
            and (d.get("arm_decisions", {}).get("llm_hybrid", {}).get("source") == "llm_fusion")
            for d in deltas)
        return {
            "decisions_total": len(deltas),
            "decision_change_count": len(changed),
            "decision_change_rate": (len(changed) / len(deltas)) if deltas else 0.0,
            "by_change_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "llm_veto_count": vetoes,
            "llm_reduce_count": reduces,
            "llm_confirmation_count": confirmations,
        }

    def portfolio_attribution(self) -> dict[str, Any]:
        """What the changed decisions COST or EARNED, from three real books."""
        metrics = {arm: compute_arm_metrics(self.books[arm]) for arm in ARMS}
        a, b, c = (metrics["quant_only"], metrics["deterministic_hybrid"],
                   metrics["llm_hybrid"])
        return {
            "arms": {arm: m.to_dict() for arm, m in metrics.items()},
            "llm_vs_quant": {
                "return_delta": c.cumulative_return - a.cumulative_return,
                "drawdown_delta": c.max_drawdown - a.max_drawdown,
                "turnover_delta": c.total_turnover - a.total_turnover,
                "cost_delta": c.total_cost - a.total_cost,
            },
            "llm_vs_deterministic": {
                "return_delta": c.cumulative_return - b.cumulative_return,
                "drawdown_delta": c.max_drawdown - b.max_drawdown,
                "turnover_delta": c.total_turnover - b.total_turnover,
                "cost_delta": c.total_cost - b.total_cost,
            },
        }

    def sample_state(self) -> dict[str, Any]:
        cycles = len(self.books["llm_hybrid"].history)
        changes = self.decision_attribution()["decision_change_count"]
        sufficient = (cycles >= MIN_CYCLES_FOR_ATTRIBUTION
                      and changes >= MIN_DECISION_CHANGES_FOR_ATTRIBUTION)
        return {
            "cycles": cycles,
            "decision_changes": changes,
            "required_cycles": MIN_CYCLES_FOR_ATTRIBUTION,
            "required_decision_changes": MIN_DECISION_CHANGES_FOR_ATTRIBUTION,
            "sufficient": sufficient,
            "verdict": "SAMPLE_OK" if sufficient else "INSUFFICIENT_SAMPLE",
        }

    def attribution(self) -> dict[str, Any]:
        """The full report. The numeric deltas are always shown — they are
        facts about three real books — but the VERDICT stays
        INSUFFICIENT_SAMPLE until the sample supports a claim. Callers and the
        UI must render `verdict`, never the raw delta, as the conclusion."""
        sample = self.sample_state()
        decision = self.decision_attribution()
        portfolio = self.portfolio_attribution()
        stats = dict(self.llm_stats)
        changes = decision["decision_change_count"]
        ret_gain = portfolio["llm_vs_deterministic"]["return_delta"]
        cost_metrics = {
            "llm_calls": stats.get("calls", 0),
            "llm_tokens": stats.get("tokens", 0),
            "llm_failures": stats.get("failures", 0),
            "llm_latency_ms_total": stats.get("latency_ms_total", 0.0),
            "tokens_per_decision_change": (stats.get("tokens", 0) / changes) if changes else None,
            "calls_per_decision_change": (stats.get("calls", 0) / changes) if changes else None,
            "tokens_per_return_point": (
                stats.get("tokens", 0) / (ret_gain * 100)
                if sample["sufficient"] and abs(ret_gain) > 1e-9 else None),
        }
        return {
            "session_id": self.session_id,
            "sample": sample,
            "verdict": sample["verdict"],
            "decision_attribution": decision,
            "portfolio_attribution": portfolio,
            "llm_cost": cost_metrics,
            "llm_stats": stats,
            "fairness": {
                "shared_price_map": True,
                "shared_cost_bps": self.books["quant_only"].cost_bps,
                "shared_sizing_policy": {
                    "base_weight": self.policy.base_weight,
                    "max_weight": self.policy.max_weight,
                    "max_gross": self.policy.max_gross,
                },
                "mark_before_rebalance": True,
                "limits": [
                    "no lot sizes, no partial fills, no intraday microstructure",
                    "cycles are scan cycles, not calendar periods — Sharpe is per-cycle",
                ],
            },
        }

    def summary(self) -> dict[str, Any]:
        """Compact per-cycle payload. Never carries an alpha claim."""
        sample = self.sample_state()
        return {
            "session_id": self.session_id,
            "arms": {arm: self.books[arm].to_dict() for arm in ARMS},
            "sample": sample,
            "sample_sufficiency": sample["verdict"],
            "decision_deltas_total": sample["decision_changes"],
            "llm_changed_decisions": sample["decision_changes"],
            "llm_stats": dict(self.llm_stats),
        }
