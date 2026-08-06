from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


class TradingMode(str, Enum):
    BACKTEST = "backtest"
    SIMULATION = "simulation"
    LIVE_PAPER = "live_paper"
    SHADOW = "shadow"
    APPROVAL_REQUIRED = "approval_required"
    LIMITED_AUTONOMOUS = "limited_autonomous"
    LIVE = "live"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Account:
    account_id: str = field(default_factory=lambda: str(uuid4()))
    account_type: str = "paper"
    currency: str = "USD"
    cash: float = 100000.0
    initial_capital: float = 100000.0
    buying_power: float = 100000.0
    equity: float = 100000.0
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id, "account_type": self.account_type,
            "currency": self.currency, "cash": round(self.cash, 2),
            "initial_capital": self.initial_capital, "buying_power": round(self.buying_power, 2),
            "equity": round(self.equity, 2), "created_at": self.created_at,
        }


@dataclass
class Position:
    position_id: str = field(default_factory=lambda: str(uuid4()))
    account_id: str = ""
    symbol: str = ""
    secid: str = ""
    name: str = ""
    market: str = ""
    currency: str = ""
    quantity: float = 0.0
    available_quantity: float = 0.0
    average_cost: float = 0.0
    market_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    opened_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    active_thesis_id: str = ""
    hold_rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id, "symbol": self.symbol,
            "secid": self.secid, "name": self.name,
            "market": self.market, "currency": self.currency,
            "quantity": self.quantity, "available_quantity": self.available_quantity,
            "average_cost": round(self.average_cost, 2),
            "market_price": round(self.market_price, 2),
            "market_value": round(self.market_value, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "weight": 0.0,  # filled below by caller with account equity
            "active_thesis_id": self.active_thesis_id,
            "hold_rationale": self.hold_rationale,
        }


@dataclass
class Order:
    order_id: str = field(default_factory=lambda: str(uuid4()))
    client_order_id: str = field(default_factory=lambda: str(uuid4()))
    account_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.MARKET
    quantity: float = 0.0
    filled_quantity: float = 0.0
    price: float = 0.0  # limit price or 0 for market
    average_fill_price: float = 0.0
    status: OrderStatus = OrderStatus.CREATED
    proposal_id: str = ""
    created_at: str = field(default_factory=_now)
    submitted_at: str = ""
    updated_at: str = field(default_factory=_now)
    commission: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id, "client_order_id": self.client_order_id,
            "symbol": self.symbol, "side": self.side.value,
            "order_type": self.order_type.value, "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "price": round(self.price, 2), "average_fill_price": round(self.average_fill_price, 2),
            "status": self.status.value, "proposal_id": self.proposal_id,
            "created_at": self.created_at, "commission": round(self.commission, 2),
            "error": self.error,
        }


@dataclass
class Execution:
    execution_id: str = field(default_factory=lambda: str(uuid4()))
    order_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: float = 0.0
    price: float = 0.0
    executed_at: str = field(default_factory=_now)
    venue: str = "virtual"

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id, "order_id": self.order_id,
            "symbol": self.symbol, "side": self.side.value,
            "quantity": self.quantity, "price": round(self.price, 2),
            "executed_at": self.executed_at, "venue": self.venue,
        }


@dataclass
class Fill:
    fill_id: str = field(default_factory=lambda: str(uuid4()))
    order_id: str = ""
    execution_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: float = 0.0
    price: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    filled_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id, "order_id": self.order_id,
            "symbol": self.symbol, "side": self.side.value,
            "quantity": self.quantity, "price": round(self.price, 2),
            "commission": round(self.commission, 2),
            "slippage": round(self.slippage, 4),
            "filled_at": self.filled_at,
        }
