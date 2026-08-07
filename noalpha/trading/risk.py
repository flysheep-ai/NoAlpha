"""Hard, deterministic pre-trade risk. External to every model and every LLM.

Nothing in this module consults a model score, a research snapshot or a fusion
decision. It is the last gate before the broker and it can only say no.

Daily-loss semantics (第七十二条). "Daily" used to mean `sum(position.realized_pnl)`
over the whole session — an all-time figure that never reset, so yesterday's
loss kept blocking today and yesterday's profit silently financed today's
blow-up. TradingDayRiskState fixes that by anchoring to a trading date and
keeping four separate, explicitly named quantities:

    realized_daily_loss     realized PnL booked SINCE this trading day opened
    equity_daily_drawdown   equity vs the start-of-day equity
    session_drawdown        equity vs the intraday peak
    (max_drawdown lives in analytics — it is a reporting concept, not a gate)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from noalpha.trading.domain import Account, Order, OrderSide, OrderStatus, OrderType, Position
from noalpha.trading.proposal import TradeAction, TradeProposal


def _utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class TradingDayRiskState:
    """Risk counters anchored to ONE trading day. Rolls on date change."""
    trading_date: str = ""
    start_of_day_equity: float = 0.0
    start_of_day_realized_pnl: float = 0.0
    peak_equity_today: float = 0.0
    equity: float = 0.0
    realized_pnl_today: float = 0.0
    unrealized_pnl_today: float = 0.0
    orders_today: int = 0
    turnover_today: float = 0.0
    rollovers: int = 0

    def observe(self, *, trading_date: str, equity: float,
                realized_pnl_total: float, unrealized_pnl_total: float = 0.0) -> bool:
        """Update the counters; returns True when a new trading day started.

        Rolling resets the anchors, so a loss booked yesterday is not a loss
        today and a profit booked yesterday cannot offset today's loss.
        """
        date = trading_date or _utc_date()
        rolled = date != self.trading_date
        if rolled:
            self.trading_date = date
            self.start_of_day_equity = equity
            self.start_of_day_realized_pnl = realized_pnl_total
            self.peak_equity_today = equity
            self.orders_today = 0
            self.turnover_today = 0.0
            self.rollovers += 1
        self.equity = equity
        self.realized_pnl_today = realized_pnl_total - self.start_of_day_realized_pnl
        self.unrealized_pnl_today = unrealized_pnl_total
        self.peak_equity_today = max(self.peak_equity_today, equity)
        return rolled

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "TradingDayRiskState":
        state = cls()
        if not payload:
            return state
        state.trading_date = str(payload.get("trading_date") or "")
        state.start_of_day_equity = float(payload.get("start_of_day_equity") or 0.0)
        state.peak_equity_today = float(payload.get("peak_equity_today") or 0.0)
        state.equity = float(payload.get("equity") or 0.0)
        state.realized_pnl_today = float(payload.get("realized_pnl_today") or 0.0)
        state.unrealized_pnl_today = float(payload.get("unrealized_pnl_today") or 0.0)
        state.orders_today = int(payload.get("orders_today") or 0)
        state.turnover_today = float(payload.get("turnover_today") or 0.0)
        state.rollovers = int(payload.get("rollovers") or 0)
        # The anchor is not serialised directly: it is reconstructed so a
        # restart mid-day resumes with today's realized loss intact.
        state.start_of_day_realized_pnl = float(
            payload.get("start_of_day_realized_pnl")
            if payload.get("start_of_day_realized_pnl") is not None
            else -state.realized_pnl_today)
        return state

    def record_order(self, notional: float) -> None:
        self.orders_today += 1
        self.turnover_today += max(notional, 0.0)

    @property
    def realized_daily_loss_pct(self) -> float:
        """Negative when today booked a realized loss. 0 before any anchor."""
        base = self.start_of_day_equity
        return (self.realized_pnl_today / base) if base > 0 else 0.0

    @property
    def equity_daily_drawdown(self) -> float:
        """Equity change since the day opened (negative = down on the day)."""
        base = self.start_of_day_equity
        return (self.equity / base - 1.0) if base > 0 else 0.0

    @property
    def session_drawdown(self) -> float:
        """Equity vs today's intraday peak (always ≤ 0)."""
        peak = self.peak_equity_today
        return (self.equity / peak - 1.0) if peak > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date,
            "start_of_day_equity": round(self.start_of_day_equity, 2),
            "start_of_day_realized_pnl": round(self.start_of_day_realized_pnl, 2),
            "equity": round(self.equity, 2),
            "peak_equity_today": round(self.peak_equity_today, 2),
            "realized_pnl_today": round(self.realized_pnl_today, 2),
            "unrealized_pnl_today": round(self.unrealized_pnl_today, 2),
            "realized_daily_loss_pct": round(self.realized_daily_loss_pct, 6),
            "equity_daily_drawdown": round(self.equity_daily_drawdown, 6),
            "session_drawdown": round(self.session_drawdown, 6),
            "orders_today": self.orders_today,
            "turnover_today": round(self.turnover_today, 2),
            "rollovers": self.rollovers,
        }


@dataclass
class RiskResult:
    approved: bool = True
    rejected: bool = False
    adjusted: bool = False
    violations: list[str] = field(default_factory=list)
    original_target_weight: float = 0.0
    adjusted_target_weight: float = 0.0
    max_position_weight: float = 0.10
    max_order_notional: float = 50000.0
    reason: str = ""
    # Structured check matrix for the UI: every check the engine actually ran,
    # with the real current value and the real limit.
    checks: list[dict[str, Any]] = field(default_factory=list)
    # The trading-day counters the daily limits were evaluated against.
    day_state: dict[str, Any] = field(default_factory=dict)

    def add_check(self, name: str, passed: bool, current: str, limit: str, detail: str = "") -> None:
        self.checks.append({"name": name, "passed": passed, "current": current,
                            "limit": limit, "detail": detail})

    def to_payload(self) -> dict[str, Any]:
        return {
            "approved": self.approved, "rejected": self.rejected, "adjusted": self.adjusted,
            "violations": self.violations, "reason": self.reason,
            "original_target_weight": self.original_target_weight,
            "adjusted_target_weight": self.adjusted_target_weight,
            "checks": self.checks,
            "day_state": self.day_state,
        }


class PositionSizingEngine:
    def __init__(self, max_position_weight: float = 0.10, max_order_notional: float = 50000.0,
                 max_single_order_pct: float = 0.25) -> None:
        self.max_position_weight = max_position_weight
        self.max_order_notional = max_order_notional
        self.max_single_order_pct = max_single_order_pct

    def size(self, proposal: TradeProposal, account: Account, current_position: Position | None,
             current_price: float) -> tuple[float, float]:
        """Returns (target_quantity, adjusted_weight)."""
        equity = account.equity

        # Max notional check
        target_notional = equity * proposal.target_weight
        if target_notional > self.max_order_notional:
            target_notional = self.max_order_notional

        # Max position weight check
        max_pos_notional = equity * self.max_position_weight
        existing = current_position.market_value if current_position else 0
        available = max_pos_notional - existing
        if target_notional > available and available > 0:
            target_notional = available

        # Max single order % of equity
        max_order_val = equity * self.max_single_order_pct
        if target_notional > max_order_val:
            target_notional = max_order_val

        qty = target_notional / current_price if current_price > 0 else 0
        adjusted_weight = target_notional / equity if equity > 0 else 0
        return qty, adjusted_weight


class PreTradeRiskEngine:
    """Hard gate. Every limit configured here is actually enforced in `check`."""

    def __init__(self, max_position_weight: float = 0.10, max_daily_loss_pct: float = 0.05,
                 max_open_orders: int = 10, max_order_notional: float = 50000.0,
                 price_deviation_pct: float = 0.10,
                 max_daily_drawdown_pct: float | None = None) -> None:
        self.max_position_weight = max_position_weight
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_open_orders = max_open_orders
        self.max_order_notional = max_order_notional
        self.price_deviation_pct = price_deviation_pct
        # Equity drawdown on the day is a SEPARATE limit from realized loss;
        # it defaults to the same tolerance but is measured and reported apart.
        self.max_daily_drawdown_pct = (max_daily_loss_pct if max_daily_drawdown_pct is None
                                       else max_daily_drawdown_pct)
        self.day_state = TradingDayRiskState()

    def check(self, proposal: TradeProposal, account: Account,
              positions: dict[str, Position], open_orders: list[Order],
              current_price: float, market_data_fresh: bool = True,
              *, trading_date: str = "", reference_price: float = 0.0,
              kill_switch: bool = False) -> RiskResult:
        violations: list[str] = []
        checks: list[dict[str, Any]] = []

        def record(name: str, passed: bool, current: str, limit: str, detail: str = "") -> None:
            checks.append({"name": name, "passed": passed, "current": current,
                           "limit": limit, "detail": detail})

        # ── roll the trading-day counters BEFORE any limit is evaluated ──
        realized_total = sum(p.realized_pnl for p in positions.values())
        unrealized_total = sum(p.unrealized_pnl for p in positions.values())
        day = self.day_state
        day.observe(trading_date=trading_date, equity=account.equity,
                    realized_pnl_total=realized_total,
                    unrealized_pnl_total=unrealized_total)

        record("market_data_fresh", market_data_fresh,
               "fresh" if market_data_fresh else "stale", "must be fresh/tradable")
        if not market_data_fresh:
            violations.append("Stale market data — cannot trade")
            return RiskResult(approved=False, rejected=True, violations=violations,
                              reason="Market data is stale", checks=checks,
                              day_state=day.to_dict())

        # Kill switch — an explicit external stop that nothing may bypass.
        halted = kill_switch or account.account_type == "halted"
        record("kill_switch", not halted,
               "engaged" if halted else "clear", "must be clear")
        if halted:
            violations.append("Kill switch engaged / account halted")
            return RiskResult(approved=False, rejected=True, violations=violations,
                              reason="Kill switch engaged — trading halted", checks=checks,
                              day_state=day.to_dict())

        # ── daily loss: TODAY's realized PnL only ──
        realized_today = day.realized_pnl_today
        limit_amount = self.max_daily_loss_pct * (day.start_of_day_equity
                                                  or account.initial_capital)
        loss_ok = realized_today >= -limit_amount
        record("realized_daily_loss", loss_ok,
               f"today {realized_today:+,.2f} ({day.realized_daily_loss_pct:+.2%})",
               f"≥ -{limit_amount:,.2f} ({self.max_daily_loss_pct:.1%} of start-of-day equity)",
               f"trading day {day.trading_date}; session total {realized_total:+,.2f}")
        if not loss_ok:
            violations.append(
                f"Daily realized loss limit reached: today={realized_today:.2f}")
            return RiskResult(
                approved=False, rejected=True, violations=violations,
                reason=f"Daily realized loss limit exceeded on {day.trading_date}: "
                       f"{realized_today:.2f}",
                checks=checks, day_state=day.to_dict())

        # ── daily equity drawdown: a distinct limit, distinctly reported ──
        dd = day.equity_daily_drawdown
        dd_ok = dd >= -self.max_daily_drawdown_pct
        record("equity_daily_drawdown", dd_ok, f"{dd:+.2%}",
               f"≥ -{self.max_daily_drawdown_pct:.1%}",
               f"start-of-day equity {day.start_of_day_equity:,.2f}")
        if not dd_ok:
            violations.append(f"Daily equity drawdown {dd:.2%} breached")
            return RiskResult(
                approved=False, rejected=True, violations=violations,
                reason=f"Daily equity drawdown {dd:.2%} breached the "
                       f"{self.max_daily_drawdown_pct:.1%} limit on {day.trading_date}",
                checks=checks, day_state=day.to_dict())

        record("session_drawdown", True, f"{day.session_drawdown:+.2%}",
               "reported, not gated",
               "distinct from the daily limits: measured against today's intraday peak")

        record("market_price", current_price > 0, f"{current_price:,.4f}", "> 0")
        if current_price <= 0:
            return RiskResult(approved=False, rejected=True,
                              violations=["No valid market price"],
                              reason="No valid market price — cannot trade", checks=checks,
                              day_state=day.to_dict())

        # ── price sanity: a tick far from the reference is a bad tick ──
        if reference_price and reference_price > 0:
            deviation = abs(current_price / reference_price - 1.0)
            dev_ok = deviation <= self.price_deviation_pct
            record("price_deviation", dev_ok, f"{deviation:.2%} vs reference",
                   f"≤ {self.price_deviation_pct:.1%}",
                   f"price {current_price:,.4f} vs reference {reference_price:,.4f}")
            if not dev_ok:
                return RiskResult(
                    approved=False, rejected=True,
                    violations=[f"Price deviates {deviation:.2%} from reference"],
                    reason=f"Price {current_price:,.4f} deviates {deviation:.2%} from the "
                           f"reference {reference_price:,.4f} — refusing to trade on a "
                           f"suspect tick",
                    checks=checks, day_state=day.to_dict())
        else:
            record("price_deviation", True, "no reference price", "skipped",
                   "no previous close available for this quote")

        # Hard rejections collected first; any hard violation kills the trade.
        hard: list[str] = list(violations)

        # Too many open orders (hard)
        active_orders = [o for o in open_orders if o.status in (
            OrderStatus.CREATED, OrderStatus.ACCEPTED, OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED, OrderStatus.ACKNOWLEDGED,
        )]
        orders_ok = len(active_orders) < self.max_open_orders
        record("open_orders", orders_ok, str(len(active_orders)), f"< {self.max_open_orders}")
        if not orders_ok:
            hard.append(f"Too many open orders: {len(active_orders)}")

        existing = positions.get(proposal.symbol)

        # SELL-side proposals: must actually hold the instrument (hard)
        if proposal.action in (TradeAction.SELL, TradeAction.REDUCE, TradeAction.CLOSE):
            has_position = bool(existing and existing.quantity > 0)
            record("position_exists", has_position,
                   f"{existing.quantity if existing else 0} shares", "> 0 to sell")
            if not has_position:
                hard.append(f"No position in {proposal.symbol} to sell")
            if hard:
                return RiskResult(approved=False, rejected=True, violations=hard,
                                  original_target_weight=proposal.target_weight,
                                  reason="; ".join(hard), checks=checks,
                                  day_state=day.to_dict())
            return RiskResult(approved=True, original_target_weight=proposal.target_weight,
                              adjusted_target_weight=proposal.target_weight,
                              reason="Sell-side checks passed", checks=checks,
                              day_state=day.to_dict())

        # BUY/INCREASE path — compute the max affordable/allowed weight and
        # either approve, downsize (adjusted) or reject when nothing fits.
        equity = account.equity if account.equity > 0 else account.cash
        requested_weight = proposal.target_weight
        existing_weight = (existing.market_value / equity) if existing and equity > 0 else 0.0

        # Position-limit headroom
        headroom = self.max_position_weight - existing_weight
        record("max_position_weight", headroom > 0.001,
               f"holding {existing_weight:.2%} + request {requested_weight:.2%}",
               f"≤ {self.max_position_weight:.2%}",
               f"headroom {max(headroom, 0):.2%}")
        if headroom <= 0.001:
            hard.append(
                f"Position weight {existing_weight:.1%} already at/above max {self.max_position_weight:.1%}"
            )

        # Cash constraint (with a small buffer for commissions/slippage)
        cash_weight = (account.cash * 0.99) / equity if equity > 0 else 0.0
        required_notional = equity * requested_weight
        record("available_cash", cash_weight > 0.001,
               f"need ≈{required_notional:,.0f}", f"cash {account.cash:,.0f}",
               "99% of cash usable (commission buffer)")
        if cash_weight <= 0.001:
            hard.append(f"Insufficient cash: have {account.cash:.0f}")

        # Notional constraint
        notional_weight = self.max_order_notional / equity if equity > 0 else 0.0
        record("max_order_notional", required_notional <= self.max_order_notional,
               f"order ≈{required_notional:,.0f}", f"≤ {self.max_order_notional:,.0f}")

        if hard:
            return RiskResult(approved=False, rejected=True, violations=hard,
                              original_target_weight=requested_weight,
                              reason="; ".join(hard), checks=checks,
                              day_state=day.to_dict())

        allowed_weight = min(requested_weight, headroom, cash_weight, notional_weight)
        if allowed_weight <= 0.001:
            return RiskResult(approved=False, rejected=True,
                              violations=[f"Allowed weight {allowed_weight:.2%} too small"],
                              original_target_weight=requested_weight,
                              reason="Constraints leave no tradable size", checks=checks,
                              day_state=day.to_dict())

        if allowed_weight < requested_weight - 1e-9:
            constraints = []
            if headroom < requested_weight:
                constraints.append(f"max position {self.max_position_weight:.0%}")
            if cash_weight < requested_weight:
                constraints.append("available cash")
            if notional_weight < requested_weight:
                constraints.append(f"max order notional {self.max_order_notional:.0f}")
            record("weight_adjustment", True,
                   f"requested {requested_weight:.2%}",
                   f"granted {allowed_weight:.2%}",
                   f"bound by {', '.join(constraints)}")
            return RiskResult(
                approved=True, adjusted=True,
                violations=[f"Requested {requested_weight:.1%} adjusted to {allowed_weight:.1%} ({', '.join(constraints)})"],
                original_target_weight=requested_weight,
                adjusted_target_weight=allowed_weight,
                reason=f"Adjusted {requested_weight:.1%} → {allowed_weight:.1%} by {', '.join(constraints)}",
                checks=checks, day_state=day.to_dict(),
            )

        return RiskResult(approved=True, original_target_weight=requested_weight,
                          adjusted_target_weight=requested_weight, reason="All checks passed",
                          checks=checks, day_state=day.to_dict())
