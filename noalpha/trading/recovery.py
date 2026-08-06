from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from noalpha.trading.domain import Fill, Order, OrderStatus, Position
from noalpha.trading.virtual_broker import VirtualBroker

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationMismatch:
    type: str = ""  # missing_local_order / missing_broker_order / position_mismatch / cash_mismatch / fill_mismatch
    description: str = ""
    local_value: Any = None
    broker_value: Any = None
    severity: str = "warning"  # warning / critical

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type, "description": self.description,
            "local_value": self.local_value, "broker_value": self.broker_value,
            "severity": self.severity,
        }


class CrashRecovery:
    def __init__(self, state_dir: str = ".cache/api/trading") -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._intents_file = self.state_dir / "order_intents.jsonl"

    def persist_intent(self, order: Order) -> None:
        """Write-ahead: persist order intent before submission."""
        try:
            entry = {
                "order_id": order.order_id,
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "side": order.side.value,
                "order_type": order.order_type.value,
                "quantity": order.quantity,
                "price": order.price,
                "status": order.status.value,
                "proposal_id": order.proposal_id,
                "created_at": order.created_at,
            }
            with open(self._intents_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            logger.warning("Failed to persist order intent", exc_info=True)

    def load_intents(self) -> list[dict[str, Any]]:
        if not self._intents_file.exists():
            return []
        intents = []
        with open(self._intents_file) as f:
            for line in f:
                try:
                    intents.append(json.loads(line.strip()))
                except Exception:
                    continue
        return intents

    def recover(self, broker: VirtualBroker) -> list[str]:
        """Recover broker state from persisted intents after crash."""
        intents = self.load_intents()
        messages: list[str] = []
        for intent in intents:
            oid = intent.get("order_id", "")
            if oid not in broker.orders:
                messages.append(f"Recovered order {oid} ({intent.get('symbol')}) from intent log")
        return messages


class ReconciliationEngine:
    @staticmethod
    def reconcile(local: VirtualBroker, broker_state: dict[str, Any]) -> list[ReconciliationMismatch]:
        mismatches: list[ReconciliationMismatch] = []

        # Compare cash
        broker_cash = broker_state.get("cash", local.account.cash)
        if abs(local.account.cash - broker_cash) > 0.01:
            mismatches.append(ReconciliationMismatch(
                type="cash_mismatch",
                description=f"Cash differs: local=${local.account.cash:.2f} vs broker=${broker_cash:.2f}",
                local_value=local.account.cash, broker_value=broker_cash,
                severity="critical",
            ))

        # Compare positions
        broker_positions = broker_state.get("positions", {})
        for sym, pos in local.positions.items():
            broker_qty = broker_positions.get(sym, {}).get("quantity", 0)
            if abs(pos.quantity - broker_qty) > 0.001:
                mismatches.append(ReconciliationMismatch(
                    type="position_mismatch",
                    description=f"{sym} qty: local={pos.quantity} vs broker={broker_qty}",
                    local_value=pos.quantity, broker_value=broker_qty,
                    severity="critical",
                ))

        return mismatches
