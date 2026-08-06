from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CounterfactualEntry:
    entry_id: str = ""
    symbol: str = ""
    decision_date: str = ""
    action: str = ""  # would_buy / would_sell / rejected / no_action
    reason: str = ""
    entry_price: float = 0.0
    current_price: float = 0.0
    return_7d: float = 0.0
    return_30d: float = 0.0
    opportunity_cost: float = 0.0
    tracked_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id, "symbol": self.symbol,
            "decision_date": self.decision_date, "action": self.action,
            "reason": self.reason,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "return_7d_pct": round(self.return_7d * 100, 2),
            "return_30d_pct": round(self.return_30d * 100, 2),
            "opportunity_cost": round(self.opportunity_cost, 2),
            "tracked_at": self.tracked_at,
        }


class CounterfactualTracker:
    def __init__(self) -> None:
        self.entries: list[CounterfactualEntry] = []

    def track_rejection(self, symbol: str, decision_date: str, action: str,
                        reason: str, entry_price: float) -> None:
        entry = CounterfactualEntry(
            entry_id=f"{symbol}-{decision_date}-{action}",
            symbol=symbol, decision_date=decision_date,
            action=action, reason=reason, entry_price=entry_price,
        )
        self.entries.append(entry)

    def update_prices(self, prices: dict[str, float]) -> None:
        for entry in self.entries:
            if entry.symbol in prices:
                current = prices[entry.symbol]
                entry.current_price = current
                if entry.entry_price > 0:
                    entry.return_7d = (current - entry.entry_price) / entry.entry_price
                    entry.return_30d = entry.return_7d
                    entry.opportunity_cost = entry.return_7d * 10000

    def get_summary(self) -> dict[str, Any]:
        entries = [e.to_dict() for e in self.entries]
        missed_gains = sum(e.return_7d for e in self.entries if e.return_7d > 0)
        avoided_losses = sum(abs(e.return_7d) for e in self.entries if e.return_7d < 0)
        return {
            "total_tracked": len(self.entries),
            "missed_gains_sum": round(missed_gains * 100, 2),
            "avoided_losses_sum": round(avoided_losses * 100, 2),
            "entries": entries,
        }
