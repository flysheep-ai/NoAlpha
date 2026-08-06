from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from noalpha.trading.domain import Fill, OrderSide


@dataclass
class PerformanceMetrics:
    total_return: float = 0.0
    annualized_return: float = 0.0
    volatility: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    calmar: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_holding_days: float = 0.0
    total_commission: float = 0.0
    total_slippage: float = 0.0
    turnover: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_return_pct": round(self.total_return * 100, 2),
            "annualized_return_pct": round(self.annualized_return * 100, 2),
            "volatility_pct": round(self.volatility * 100, 2),
            "sharpe": round(self.sharpe, 2),
            "sortino": round(self.sortino, 2),
            "max_drawdown_pct": round(self.max_drawdown * 100, 2),
            "calmar": round(self.calmar, 2),
            "win_rate_pct": round(self.win_rate * 100, 1),
            "profit_factor": round(self.profit_factor, 2),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "avg_holding_days": round(self.avg_holding_days, 1),
            "total_commission": round(self.total_commission, 2),
            "total_slippage": round(self.total_slippage, 2),
        }


@dataclass
class AIMetrics:
    proposal_count: int = 0
    execution_rate: float = 0.0
    rejected_by_risk: int = 0
    adjusted_by_risk: int = 0
    thesis_success_rate: float = 0.0
    invalidation_accuracy: float = 0.0
    confidence_calibration: dict[str, float] = field(default_factory=dict)
    citation_support_rate: float = 0.0
    unsupported_claim_rate: float = 0.0
    no_action_tracked: int = 0
    counterfactual_opportunity_cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_count": self.proposal_count,
            "execution_rate_pct": round(self.execution_rate * 100, 1),
            "rejected_by_risk": self.rejected_by_risk,
            "adjusted_by_risk": self.adjusted_by_risk,
            "thesis_success_rate_pct": round(self.thesis_success_rate * 100, 1),
            "invalidation_accuracy_pct": round(self.invalidation_accuracy * 100, 1),
            "confidence_calibration": self.confidence_calibration,
            "citation_support_rate": round(self.citation_support_rate, 2),
            "unsupported_claim_rate": round(self.unsupported_claim_rate, 2),
            "no_action_tracked": self.no_action_tracked,
            "counterfactual_opportunity_cost": self.counterfactual_opportunity_cost,
        }


class PerformanceAnalyzer:
    @staticmethod
    def analyze(
        equity_curve: list[float],
        fills: list[Fill],
        trades: list[dict[str, Any]],
        risk_free_rate: float = 0.03,
    ) -> PerformanceMetrics:
        if not equity_curve or len(equity_curve) < 2:
            return PerformanceMetrics()

        # Returns
        initial = equity_curve[0]
        final = equity_curve[-1]
        total_return = (final - initial) / initial if initial > 0 else 0

        # Daily returns
        returns = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] > 0:
                returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])

        n = len(returns)
        if n == 0:
            return PerformanceMetrics(total_return=total_return)

        # Volatility (annualized)
        mean_ret = sum(returns) / n
        variance = sum((r - mean_ret) ** 2 for r in returns) / n
        vol = math.sqrt(variance) * math.sqrt(252)

        # Sharpe
        excess = mean_ret - risk_free_rate / 252
        sharpe = (excess / math.sqrt(variance)) * math.sqrt(252) if variance > 0 else 0

        # Sortino
        downside = [r for r in returns if r < 0]
        downside_var = sum((r - 0) ** 2 for r in downside) / max(len(downside), 1)
        downside_std = math.sqrt(downside_var) * math.sqrt(252)
        sortino = (total_return - risk_free_rate) / downside_std if downside_std > 0 else 0

        # Max drawdown
        peak = equity_curve[0]
        max_dd = 0.0
        for val in equity_curve:
            if val > peak:
                peak = val
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd

        # Calmar
        calmar = total_return / max_dd if max_dd > 0 else 0

        # Win/Loss
        wins = [t for t in trades if t.get("realized_pnl", 0) > 0]
        losses = [t for t in trades if t.get("realized_pnl", 0) < 0]
        win_rate = len(wins) / len(trades) if trades else 0

        total_wins = sum(t.get("realized_pnl", 0) for t in wins)
        total_losses = abs(sum(t.get("realized_pnl", 0) for t in losses))
        profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")

        # Commission + slippage
        total_comm = sum(f.commission for f in fills)
        total_slip = sum(f.slippage * f.quantity * f.price for f in fills)

        return PerformanceMetrics(
            total_return=total_return,
            annualized_return=total_return,  # Simplified: assume ~1 period
            volatility=vol,
            sharpe=sharpe,
            sortino=sortino,
            max_drawdown=max_dd,
            calmar=calmar,
            win_rate=win_rate,
            profit_factor=profit_factor,
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            avg_win=total_wins / len(wins) if wins else 0,
            avg_loss=-(total_losses / len(losses)) if losses else 0,
            total_commission=total_comm,
            total_slippage=total_slip,
        )


class AIAnalyzer:
    @staticmethod
    def analyze(proposals: list[dict[str, Any]], theses: list[dict[str, Any]],
                trades: list[dict[str, Any]], no_actions: list[dict[str, Any]]) -> AIMetrics:
        m = AIMetrics()
        m.proposal_count = len(proposals)

        executed = [p for p in proposals if p.get("status") == "executed"]
        rejected_risk = [p for p in proposals if "rejected" in str(p.get("risk_result", {}).get("approved", "")).lower()
                         or (p.get("status") == "rejected" and p.get("risk_result", {}).get("approved") == False)]
        adjusted = [p for p in proposals if p.get("risk_result", {}).get("adjusted")]

        m.execution_rate = len(executed) / max(len(proposals), 1)
        m.rejected_by_risk = len(rejected_risk)
        m.adjusted_by_risk = len(adjusted)

        # Thesis success
        closed_theses = [t for t in theses if t.get("status") in ("closed", "invalidated")]
        successful = [t for t in closed_theses if t.get("realized_pnl", 0) > 0]
        m.thesis_success_rate = len(successful) / max(len(closed_theses), 1)

        # Invalidation accuracy
        invalidated = [t for t in theses if t.get("status") == "invalidated"]
        correctly_invalidated = [t for t in invalidated if t.get("realized_pnl", 0) <= 0]
        m.invalidation_accuracy = len(correctly_invalidated) / max(len(invalidated), 1)

        m.no_action_tracked = len(no_actions)

        # Confidence calibration
        buckets: dict[str, list[float]] = {"0.5-0.6": [], "0.6-0.7": [], "0.7-0.8": [], "0.8-0.9": [], "0.9-1.0": []}
        for t in trades:
            conf = t.get("confidence", 0.5)
            won = t.get("realized_pnl", 0) > 0
            if conf < 0.6: buckets["0.5-0.6"].append(1.0 if won else 0.0)
            elif conf < 0.7: buckets["0.6-0.7"].append(1.0 if won else 0.0)
            elif conf < 0.8: buckets["0.7-0.8"].append(1.0 if won else 0.0)
            elif conf < 0.9: buckets["0.8-0.9"].append(1.0 if won else 0.0)
            else: buckets["0.9-1.0"].append(1.0 if won else 0.0)
        for k, v in buckets.items():
            if v:
                m.confidence_calibration[k] = sum(v) / len(v)

        return m
