"""BrokerAdapter — the interface every real broker must implement.

LLM is NEVER allowed to hold a reference to a BrokerAdapter.
Only the Trading Execution System (OMS) may call through this interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BrokerMode(str, Enum):
    SIMULATION = "simulation"
    BROKER_PAPER = "broker_paper"
    LIVE_APPROVAL_REQUIRED = "live_approval_required"
    LIVE_LIMITED_AUTONOMOUS = "live_limited_autonomous"
    LIVE_READ_ONLY = "live_read_only"


@dataclass
class BrokerCapabilities:
    broker_name: str
    markets: list[str] = field(default_factory=list)
    paper_supported: bool = False
    live_supported: bool = False
    order_types: list[str] = field(default_factory=lambda: ["market", "limit"])
    time_in_force: list[str] = field(default_factory=lambda: ["day"])
    fractional: bool = False
    shorting: bool = False
    stream_orders: bool = False
    stream_fills: bool = False
    replace_order: bool = False
    extended_hours: bool = False
    currency: str = "USD"
    requires_desktop_client: bool = False
    mode: BrokerMode = BrokerMode.SIMULATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker_name": self.broker_name, "markets": self.markets,
            "paper_supported": self.paper_supported, "live_supported": self.live_supported,
            "order_types": self.order_types, "time_in_force": self.time_in_force,
            "fractional": self.fractional, "shorting": self.shorting,
            "stream_orders": self.stream_orders, "stream_fills": self.stream_fills,
            "replace_order": self.replace_order, "extended_hours": self.extended_hours,
            "currency": self.currency, "requires_desktop_client": self.requires_desktop_client,
            "mode": self.mode.value,
        }


class BrokerAdapter(ABC):
    """Real broker integration interface. Only OMS calls through this."""

    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        ...

    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def health(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_account(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_buying_power(self) -> float:
        ...

    @abstractmethod
    def get_positions(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_open_orders(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_order(self, broker_order_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def submit_order(self, intent: dict[str, Any]) -> dict[str, Any]:
        ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def replace_order(self, broker_order_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        ...

    def reconcile(self, local_state: dict[str, Any]) -> list[dict[str, Any]]:
        return []
