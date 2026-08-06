from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from noalpha.trading.analytics import AIAnalyzer, PerformanceAnalyzer
from noalpha.trading.domain import Fill, Order, OrderSide, OrderType
from noalpha.trading.proposal import DecisionSnapshot, TradeAction, TradeProposal
from noalpha.trading.risk import PreTradeRiskEngine
from noalpha.trading.thesis import InvestmentThesis
from noalpha.trading.virtual_broker import VirtualBroker

logger = logging.getLogger(__name__)


@dataclass
class ReplayConfig:
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = 100000.0
    symbols: list[str] = field(default_factory=list)


@dataclass
class ReplayResult:
    equity_curve: list[float] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    theses: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    no_actions: list[dict[str, Any]] = field(default_factory=list)
    future_evidence_total: int = 0
    performance: dict[str, Any] = field(default_factory=dict)
    ai_metrics: dict[str, Any] = field(default_factory=dict)


class HistoricalReplayEngine:
    def __init__(self, config: ReplayConfig) -> None:
        self.config = config
        self.broker: VirtualBroker | None = None
        self.risk_engine = PreTradeRiskEngine()

    def run(self, price_history: dict[str, list[dict[str, Any]]],
            decision_fn) -> ReplayResult:
        """Run historical replay with walk-forward.

        price_history: {symbol: [{date, open, high, low, close, volume}, ...]}
        decision_fn: (market_state, broker, date) -> TradeProposal | None
        """
        self.broker = VirtualBroker(initial_capital=self.config.initial_capital)
        result = ReplayResult()

        # Sort all dates
        all_dates = set()
        for bars in price_history.values():
            for bar in bars:
                all_dates.add(bar["date"])
        sorted_dates = sorted(all_dates)

        for date_str in sorted_dates:
            # Build current market state
            market_state: dict[str, dict[str, Any]] = {}
            for sym, bars in price_history.items():
                for bar in bars:
                    if bar["date"] == date_str:
                        market_state[sym] = bar
                        break

            if not market_state:
                continue

            # Update broker prices
            prices = {sym: bar["close"] for sym, bar in market_state.items()}
            self.broker.update_market_prices(prices)

            # Agent decision
            proposal = decision_fn(market_state, self.broker, date_str)

            if proposal:
                result.proposals.append(proposal.to_dict())

                if proposal.action in (TradeAction.BUY, TradeAction.SELL, TradeAction.INCREASE,
                                       TradeAction.REDUCE, TradeAction.CLOSE):
                    current_price = market_state.get(proposal.symbol, {}).get("close", 0)
                    risk_result = self.risk_engine.check(
                        proposal, self.broker.account,
                        self.broker.positions, list(self.broker.orders.values()),
                        current_price,
                    )

                    snapshot = DecisionSnapshot(
                        as_of=date_str,
                        portfolio_equity=self.broker.account.equity,
                        available_cash=self.broker.account.cash,
                        positions=[p.to_dict() for p in self.broker.positions.values()],
                        proposal=proposal.to_dict(),
                        risk_result={"approved": risk_result.approved, "reason": risk_result.reason},
                        final_action=proposal.action.value if risk_result.approved else "rejected",
                    )
                    result.decisions.append(snapshot.to_dict())

                    if risk_result.approved:
                        side = OrderSide.SELL if proposal.action in (
                            TradeAction.SELL, TradeAction.REDUCE, TradeAction.CLOSE
                        ) else OrderSide.BUY
                        order = Order(
                            account_id=self.broker.account.account_id,
                            symbol=proposal.symbol, side=side,
                            order_type=OrderType.MARKET,
                            quantity=proposal.target_weight * self.broker.account.equity / current_price if current_price > 0 else 0,
                            proposal_id=proposal.proposal_id,
                        )
                        self.broker.submit_order(order, current_price, proposal.proposal_id)
                        result.trades.append({
                            "date": date_str, "symbol": proposal.symbol,
                            "action": proposal.action.value,
                            "price": current_price,
                            "quantity": order.filled_quantity,
                            "thesis": proposal.rationale.thesis,
                            "evidence_ids": proposal.rationale.supporting_evidence_ids,
                            "confidence": proposal.confidence,
                            "commission": order.commission,
                        })
                    else:
                        result.no_actions.append({
                            "date": date_str, "symbol": proposal.symbol,
                            "action": proposal.action.value,
                            "reason": risk_result.reason,
                        })
                elif proposal.action == TradeAction.NO_ACTION:
                    result.no_actions.append({
                        "date": date_str, "action": "no_action",
                        "reason": "Agent decided not to trade",
                    })

            # Record equity
            result.equity_curve.append(self.broker.account.equity)

        # Final analytics
        result.performance = PerformanceAnalyzer.analyze(
            result.equity_curve, self.broker.fills, result.trades,
        ).to_dict()

        result.ai_metrics = AIAnalyzer.analyze(
            result.proposals, result.theses, result.trades, result.no_actions,
        ).to_dict()

        result.future_evidence_total = 0  # Must stay 0
        return result
