from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from noalpha.trading.domain import Account, Order, OrderSide, OrderStatus, OrderType, Position
from noalpha.trading.proposal import TradeAction, TradeProposal


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
    def __init__(self, max_position_weight: float = 0.10, max_daily_loss_pct: float = 0.05,
                 max_open_orders: int = 10, max_order_notional: float = 50000.0,
                 price_deviation_pct: float = 0.10) -> None:
        self.max_position_weight = max_position_weight
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_open_orders = max_open_orders
        self.max_order_notional = max_order_notional
        self.price_deviation_pct = price_deviation_pct
        self._daily_pnl = 0.0

    def check(self, proposal: TradeProposal, account: Account,
              positions: dict[str, Position], open_orders: list[Order],
              current_price: float, market_data_fresh: bool = True) -> RiskResult:
        violations: list[str] = []
        checks: list[dict[str, Any]] = []

        def record(name: str, passed: bool, current: str, limit: str, detail: str = "") -> None:
            checks.append({"name": name, "passed": passed, "current": current,
                           "limit": limit, "detail": detail})

        record("market_data_fresh", market_data_fresh,
               "fresh" if market_data_fresh else "stale", "must be fresh/tradable")
        if not market_data_fresh:
            violations.append("Stale market data — cannot trade")
            return RiskResult(approved=False, rejected=True, violations=violations,
                              reason="Market data is stale", checks=checks)

        # Kill switch check
        halted = account.account_type == "halted"
        record("account_active", not halted, account.account_type or "active", "not halted")
        if halted:
            violations.append("Account halted")

        # Daily loss limit (only check realized losses + current drawdown)
        total_realized = sum(p.realized_pnl for p in positions.values())
        limit_amount = self.max_daily_loss_pct * account.initial_capital
        loss_ok = total_realized >= -limit_amount
        record("daily_loss_limit", loss_ok,
               f"realized {total_realized:+,.2f}",
               f"≥ -{limit_amount:,.2f} ({self.max_daily_loss_pct:.1%} of capital)")
        if not loss_ok:
            violations.append(f"Daily loss limit reached: realized=${total_realized:.2f}")
            return RiskResult(approved=False, rejected=True, violations=violations,
                              reason=f"Daily loss limit exceeded: realized=${total_realized:.2f}",
                              checks=checks)

        record("market_price", current_price > 0, f"{current_price:,.4f}", "> 0")
        if current_price <= 0:
            return RiskResult(approved=False, rejected=True,
                              violations=["No valid market price"],
                              reason="No valid market price — cannot trade", checks=checks)

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
                                  reason="; ".join(hard), checks=checks)
            return RiskResult(approved=True, original_target_weight=proposal.target_weight,
                              adjusted_target_weight=proposal.target_weight,
                              reason="Sell-side checks passed", checks=checks)

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
                              reason="; ".join(hard), checks=checks)

        allowed_weight = min(requested_weight, headroom, cash_weight, notional_weight)
        if allowed_weight <= 0.001:
            return RiskResult(approved=False, rejected=True,
                              violations=[f"Allowed weight {allowed_weight:.2%} too small"],
                              original_target_weight=requested_weight,
                              reason="Constraints leave no tradable size", checks=checks)

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
                checks=checks,
            )

        return RiskResult(approved=True, original_target_weight=requested_weight,
                          adjusted_target_weight=requested_weight, reason="All checks passed",
                          checks=checks)
