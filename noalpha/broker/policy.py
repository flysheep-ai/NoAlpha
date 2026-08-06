"""LiveTradingPolicy — hard constraints between AI and real money.

AI can NEVER modify its own policy. Only user configuration can change it.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class LiveTradingMode(str, Enum):
    SIMULATION = "simulation"
    BROKER_PAPER = "broker_paper"
    LIVE_APPROVAL_REQUIRED = "live_approval_required"
    LIVE_LIMITED_AUTONOMOUS = "live_limited_autonomous"
    LIVE_DISABLED = "live_disabled"


class KillSwitchLevel(str, Enum):
    NONE = "none"
    GLOBAL = "global"         # all trading halted
    ACCOUNT = "account"       # one account halted
    MARKET = "market"          # e.g. CN halted
    STRATEGY = "strategy"      # e.g. momentum halted
    INSTRUMENT = "instrument"  # single symbol halted


@dataclass
class LiveTradingPolicy:
    mode: LiveTradingMode = LiveTradingMode.SIMULATION
    live_trading_enabled: bool = False  # NEVER auto-enable; user must explicitly opt in

    # ── kill switches ──
    kill_switches: dict[str, bool] = field(default_factory=lambda: {
        "global": False, "account": False, "market": False,
        "strategy": False, "instrument": False,
    })

    # ── risk bounds ──
    max_order_notional: float = 5000.0
    max_position_weight: float = 0.10
    max_gross_exposure: float = 1.0
    max_net_exposure: float = 0.8
    max_daily_loss: float = 1000.0
    max_drawdown: float = 0.10
    max_orders_per_day: int = 20
    max_orders_per_hour: int = 5
    max_turnover: float = 100000.0
    allow_extended_hours: bool = False
    require_approval_above: float = 500.0

    # ── quality gates ──
    minimum_confidence: float = 0.6
    minimum_evidence_quality: str = "signal_only"
    maximum_market_data_age_seconds: float = 30.0
    max_price_deviation: float = 0.02

    # ── allowed universe ──
    allowed_accounts: list[str] = field(default_factory=list)
    allowed_markets: list[str] = field(default_factory=lambda: ["CN", "US"])
    allowed_instruments: list[str] = field(default_factory=list)  # empty = all allowed
    blocked_instruments: list[str] = field(default_factory=list)
    allowed_order_types: list[str] = field(default_factory=lambda: ["market", "limit"])

    # ── live controls (user configurable only) ──
    paper_api_key: str = ""
    live_api_key: str = ""
    paper_api_secret: str = ""
    live_api_secret: str = ""
    paper_base_url: str = ""
    live_base_url: str = ""

    # ── daily tracking ──
    _daily_pnl: float = 0.0
    _daily_orders: int = 0
    _last_reset_date: str = ""
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def allows_order(self, intent: Any) -> bool:
        """Hard gate: returns True only if ALL checks pass."""
        if not self.live_trading_enabled and self.mode not in (LiveTradingMode.SIMULATION, LiveTradingMode.BROKER_PAPER):
            return False
        if self.kill_switches.get("global", False):
            return False
        if self.kill_switches.get("account", False):
            return False
        if intent.symbol in self.blocked_instruments:
            return False
        if self.allowed_instruments and intent.symbol not in self.allowed_instruments:
            return False
        order_notional = intent.quantity * (intent.limit_price or 100.0)
        if order_notional > self.max_order_notional:
            return False
        # daily limit check
        self._maybe_reset_daily()
        if self._daily_orders >= self.max_orders_per_day:
            return False
        if intent.side == "buy" and intent.order_type not in self.allowed_order_types:
            return False
        return True

    def record_order(self, notional: float = 0.0) -> None:
        with self._lock:
            self._daily_orders += 1

    def record_pnl(self, pnl: float) -> None:
        with self._lock:
            self._daily_pnl += pnl

    def daily_loss_exceeded(self) -> bool:
        return abs(self._daily_pnl) >= self.max_daily_loss

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._daily_orders = 0
            self._daily_pnl = 0.0
            self._last_reset_date = today

    def activate_kill_switch(self, level: KillSwitchLevel, reason: str = "") -> None:
        self.kill_switches[level.value] = True
        if level == KillSwitchLevel.GLOBAL:
            for k in self.kill_switches:
                self.kill_switches[k] = True

    def deactivate_kill_switch(self, level: KillSwitchLevel) -> None:
        self.kill_switches[level.value] = False

    def to_dict(self, include_secrets: bool = False) -> dict[str, Any]:
        d = {
            "mode": self.mode.value, "live_trading_enabled": self.live_trading_enabled,
            "kill_switches": self.kill_switches,
            "max_order_notional": self.max_order_notional,
            "max_position_weight": self.max_position_weight,
            "max_daily_loss": self.max_daily_loss,
            "max_drawdown": self.max_drawdown,
            "max_orders_per_day": self.max_orders_per_day,
            "max_orders_per_hour": self.max_orders_per_hour,
            "require_approval_above": self.require_approval_above,
            "minimum_confidence": self.minimum_confidence,
            "maximum_market_data_age_seconds": self.maximum_market_data_age_seconds,
            "max_price_deviation": self.max_price_deviation,
            "allowed_instruments": self.allowed_instruments,
            "blocked_instruments": self.blocked_instruments,
            "daily_orders": self._daily_orders,
            "daily_pnl": round(self._daily_pnl, 2),
        }
        if include_secrets:
            d |= {
                "paper_api_key": "***" if self.paper_api_key else "",
                "live_api_key": "***" if self.live_api_key else "",
            }
        return d

    def to_safe_dict(self) -> dict[str, Any]:
        """NEVER includes secrets. Safe for frontend / LLM context."""
        return self.to_dict(include_secrets=False)
