from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from noalpha.trading.domain import (
    Account, Execution, Fill, Order, OrderSide, OrderStatus, OrderType, Position, _now,
)
from noalpha.trading.proposal import ExitReason, TradeProposal

logger = logging.getLogger(__name__)

SLIPPAGE_BPS = 5  # 0.05% default slippage
COMMISSION_PER_SHARE = 0.005  # $0.005/share


class MatchingEngine:
    """Simple bar/quote-based fill simulator."""

    def match_market(self, order: Order, current_price: float, current_volume: float = 0) -> list[Fill]:
        if current_price <= 0:
            return []
        slippage = current_price * SLIPPAGE_BPS / 10000
        fill_price = current_price + slippage if order.side == OrderSide.BUY else current_price - slippage

        fills: list[Fill] = []
        remaining = order.quantity - order.filled_quantity
        fill_qty = remaining  # Full fill for market orders
        if fill_qty <= 0:
            return fills

        commission = fill_qty * COMMISSION_PER_SHARE
        fill = Fill(
            order_id=order.order_id, symbol=order.symbol, side=order.side,
            quantity=fill_qty, price=fill_price, commission=commission,
            slippage=slippage,
        )
        fills.append(fill)
        return fills

    def match_limit(self, order: Order, current_price: float, current_high: float = 0, current_low: float = 0) -> list[Fill]:
        limit = order.price
        fills: list[Fill] = []

        if order.side == OrderSide.BUY and current_low > 0 and current_low <= limit:
            fill_price = min(current_price, limit)
        elif order.side == OrderSide.SELL and current_high > 0 and current_high >= limit:
            fill_price = max(current_price, limit)
        else:
            return fills  # Not triggered

        remaining = order.quantity - order.filled_quantity
        if remaining <= 0:
            return fills

        commission = remaining * COMMISSION_PER_SHARE
        fill = Fill(
            order_id=order.order_id, symbol=order.symbol, side=order.side,
            quantity=remaining, price=fill_price, commission=commission,
        )
        fills.append(fill)
        return fills


class VirtualBroker:
    def __init__(self, initial_cash: float = 100000.0, currency: str = "USD") -> None:
        self.account = Account(
            cash=initial_cash, initial_capital=initial_cash,
            buying_power=initial_cash, equity=initial_cash, currency=currency,
        )
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.executions: list[Execution] = []
        self.decisions: list[dict[str, Any]] = []
        self.engine = MatchingEngine()
        self._kill_switch = False
        self._daily_pnl = 0.0
        self._daily_loss_limit = float("inf")

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch

    def activate_kill_switch(self, reason: str = "") -> None:
        self._kill_switch = True
        logger.warning("KILL SWITCH ACTIVATED: %s", reason)

    def submit_order(self, order: Order, current_price: float, proposal_id: str = "",
                     current_high: float = 0, current_low: float = 0,
                     instrument_meta: dict[str, str] | None = None) -> Order:
        meta = instrument_meta or {}
        if self._kill_switch:
            order.status = OrderStatus.REJECTED
            order.error = "Kill switch active"
            self.orders[order.order_id] = order
            return order

        if order.side == OrderSide.SELL:
            # No short selling in the simulation account: clamp to holdings.
            held = self.positions.get(order.symbol)
            available = held.available_quantity if held else 0.0
            if available <= 0:
                order.status = OrderStatus.REJECTED
                order.error = f"No position in {order.symbol} to sell"
                self.orders[order.order_id] = order
                return order
            if order.quantity > available:
                order.quantity = available

        order.status = OrderStatus.ACCEPTED
        order.submitted_at = _now()
        order.proposal_id = proposal_id

        # Get fills
        if order.order_type == OrderType.MARKET:
            fills = self.engine.match_market(order, current_price)
        else:
            fills = self.engine.match_limit(order, current_price, current_high, current_low)

        if fills:
            for fill in fills:
                self.fills.append(fill)
                exec_ = Execution(
                    order_id=order.order_id, symbol=order.symbol, side=order.side,
                    quantity=fill.quantity, price=fill.price, venue="virtual",
                )
                self.executions.append(exec_)
                fill.execution_id = exec_.execution_id

                order.filled_quantity += fill.quantity
                order.commission += fill.commission
                self._update_position(order, fill,
                                      secid=meta.get("secid", ""),
                                      name=meta.get("name", ""),
                                      market=meta.get("market", ""),
                                      currency=meta.get("currency", ""),)

                if order.side == OrderSide.BUY:
                    self.account.cash -= fill.quantity * fill.price
                else:
                    self.account.cash += fill.quantity * fill.price
                self.account.cash -= fill.commission

            order.average_fill_price = (
                sum(f.price * f.quantity for f in fills) / sum(f.quantity for f in fills)
            )
            if order.filled_quantity >= order.quantity:
                order.status = OrderStatus.FILLED
            else:
                order.status = OrderStatus.PARTIALLY_FILLED
        else:
            order.status = OrderStatus.REJECTED
            order.error = "No fill generated"

        order.updated_at = _now()
        self.orders[order.order_id] = order
        self._recalc_account()
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self.orders.get(order_id)
        if order and order.status in (OrderStatus.CREATED, OrderStatus.ACCEPTED, OrderStatus.SUBMITTED):
            order.status = OrderStatus.CANCELLED
            order.updated_at = _now()
        return order or Order()

    def _update_position(self, order: Order, fill: Fill,
                         secid: str = "", name: str = "", market: str = "",
                         currency: str = "") -> None:
        key = order.symbol
        pos = self.positions.get(key)
        if not pos:
            pos = Position(
                account_id=self.account.account_id, symbol=order.symbol,
                secid=secid, name=name, market=market, currency=currency,
            )
            self.positions[key] = pos

        if order.side == OrderSide.BUY:
            total_cost = pos.average_cost * pos.quantity + fill.price * fill.quantity
            pos.quantity += fill.quantity
            pos.available_quantity = pos.quantity
            pos.average_cost = total_cost / pos.quantity if pos.quantity else 0
        else:
            if pos.quantity > 0:
                realized = (fill.price - pos.average_cost) * min(fill.quantity, pos.quantity)
                pos.realized_pnl += realized
                pos.quantity -= fill.quantity
                pos.available_quantity = pos.quantity
                if pos.quantity <= 0:
                    pos.average_cost = 0
                    pos.quantity = 0
                    pos.available_quantity = 0
        # Mark to the fill price immediately so equity does not dip by the
        # purchase amount until the next external price update.
        pos.market_price = fill.price
        pos.market_value = pos.quantity * fill.price
        pos.unrealized_pnl = (fill.price - pos.average_cost) * pos.quantity if pos.quantity > 0 else 0.0
        pos.updated_at = _now()

    def update_market_prices(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            if symbol in self.positions:
                pos = self.positions[symbol]
                pos.market_price = price
                pos.market_value = pos.quantity * price
                if pos.quantity > 0:
                    pos.unrealized_pnl = (price - pos.average_cost) * pos.quantity
            else:
                # Price update for watch-only symbols
                pass
        self._recalc_account()

    def _recalc_account(self) -> None:
        # Realized PnL already flows through cash on fills; equity is
        # simply cash + marked position value (no double counting).
        market_value = sum(p.market_value for p in self.positions.values())
        self.account.equity = self.account.cash + market_value
        self.account.buying_power = self.account.cash  # cash account, no margin
        self._daily_pnl = self.account.equity - self.account.initial_capital

    def to_state(self) -> dict[str, Any]:
        return {
            "account": self.account.to_dict(),
            "positions": {k: p.to_dict() | {"opened_at": p.opened_at, "account_id": p.account_id}
                          for k, p in self.positions.items()},
            "orders": [o.to_dict() for o in self.orders.values()],
            "fills": [f.to_dict() for f in self.fills],
            "decisions": self.decisions,
            "kill_switch": self._kill_switch,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "VirtualBroker":
        acc = state.get("account", {})
        broker = cls(initial_cash=acc.get("initial_capital", 100000.0),
                     currency=acc.get("currency", "USD"))
        broker.account.cash = acc.get("cash", broker.account.cash)
        broker.account.equity = acc.get("equity", broker.account.equity)
        broker.account.account_id = acc.get("account_id", broker.account.account_id)
        for key, p in (state.get("positions") or {}).items():
            pos = Position(
                account_id=p.get("account_id", ""), symbol=p.get("symbol", key),
                secid=p.get("secid", ""), name=p.get("name", ""),
                market=p.get("market", ""), currency=p.get("currency", ""),
                quantity=p.get("quantity", 0.0), available_quantity=p.get("available_quantity", 0.0),
                average_cost=p.get("average_cost", 0.0), market_price=p.get("market_price", 0.0),
                market_value=p.get("market_value", 0.0), unrealized_pnl=p.get("unrealized_pnl", 0.0),
                realized_pnl=p.get("realized_pnl", 0.0), active_thesis_id=p.get("active_thesis_id", ""),
                hold_rationale=p.get("hold_rationale", ""),
            )
            broker.positions[key] = pos
        for o in state.get("orders") or []:
            order = Order(
                order_id=o["order_id"], symbol=o.get("symbol", ""),
                side=OrderSide(o.get("side", "buy")), order_type=OrderType(o.get("order_type", "market")),
                quantity=o.get("quantity", 0.0), filled_quantity=o.get("filled_quantity", 0.0),
                average_fill_price=o.get("average_fill_price", 0.0),
                status=OrderStatus(o.get("status", "created")), proposal_id=o.get("proposal_id", ""),
                created_at=o.get("created_at", ""), commission=o.get("commission", 0.0),
            )
            broker.orders[order.order_id] = order
        for f in state.get("fills") or []:
            broker.fills.append(Fill(
                fill_id=f["fill_id"], order_id=f.get("order_id", ""), symbol=f.get("symbol", ""),
                side=OrderSide(f.get("side", "buy")), quantity=f.get("quantity", 0.0),
                price=f.get("price", 0.0), commission=f.get("commission", 0.0),
                filled_at=f.get("filled_at", ""),
            ))
        broker.decisions = state.get("decisions") or []
        broker._kill_switch = bool(state.get("kill_switch"))
        broker._recalc_account()
        return broker

    def get_portfolio_summary(self) -> dict[str, Any]:
        equity = self.account.equity
        position_dicts = []
        for p in self.positions.values():
            if p.quantity <= 0:
                continue
            d = p.to_dict()
            d["weight"] = round(p.market_value / equity, 4) if equity > 0 else 0.0
            position_dicts.append(d)
        return {
            "account": self.account.to_dict(),
            "positions": position_dicts,
            "orders": [o.to_dict() for o in list(self.orders.values())[-20:]],
            "fills": [f.to_dict() for f in self.fills[-20:]],
            "equity": round(self.account.equity, 2),
            "cash": round(self.account.cash, 2),
            "market_value": round(sum(p.market_value for p in self.positions.values()), 2),
            "total_pnl": round(self._daily_pnl, 2),
            "total_return": round(self._daily_pnl / self.account.initial_capital * 100, 2) if self.account.initial_capital else 0,
            "kill_switch": self._kill_switch,
            "trade_count": len(self.fills),
        }
