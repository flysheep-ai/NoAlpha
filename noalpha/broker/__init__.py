"""Broker Gateway — the controlled boundary between AI and real money.

LLM never touches this module directly. Trading Execution System only.
"""
from noalpha.broker.adapter import BrokerAdapter, BrokerCapabilities
from noalpha.broker.oms import OrderIntent, OrderManagementSystem, OrderIntentStatus
from noalpha.broker.policy import LiveTradingPolicy, LiveTradingMode, KillSwitchLevel
from noalpha.broker.alpaca_adapter import AlpacaAdapter

__all__ = [
    "BrokerAdapter", "BrokerCapabilities",
    "OrderIntent", "OrderManagementSystem", "OrderIntentStatus",
    "LiveTradingPolicy", "LiveTradingMode", "KillSwitchLevel",
    "AlpacaAdapter",
]
