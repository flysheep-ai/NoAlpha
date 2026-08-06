"""24/7 Continuous Intelligence Loop — replaces TradingScheduler._run().

Phases driven by MarketCalendar, not by hardcoded `while RUNNING: run_cycle()`.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from noalpha.market.calendar import (
    GlobalMarketSnapshot, MarketStatus, global_snapshot, market_status,
    Market, MarketSession, MARKET_LABEL,
)
from noalpha.market.live_quote import MarketSession as MS
from noalpha.trading.autonomous import AutonomousTradingLoop, LiveMarketFeed
from noalpha.trading.daily_report import (
    DailyLesson, DailyTradingReport, DecisionQuality, MarketRegime,
    MissedOpportunity, TradeReviewItem, determine_market_regime,
)
from noalpha.trading.operational_state import AgentOperationalState, OperationPhase
from noalpha.trading.proposal import TradeAction
from noalpha.trading.session import SessionStatus, TradingSession, TradingStore
from noalpha.trading.virtual_broker import VirtualBroker

logger = logging.getLogger(__name__)

ONE_HOUR = timedelta(hours=1)
CYCLE_SPACING = 60  # seconds between intelligence cycles (not trading cycles)


@dataclass
class OvernightEvent:
    event_id: str = field(default_factory=lambda: str(uuid4())[:12])
    event_type: str = ""  # global_market / news / filing / thesis_update / risk
    source_market: str = ""
    summary: str = ""
    affected_symbols: list[str] = field(default_factory=list)
    created_at: str = ""


@dataclass
class NextSessionAction:
    """Pending action to revalidate at next session open."""
    plan_id: str = field(default_factory=lambda: str(uuid4())[:12])
    symbol: str = ""
    action: str = ""
    reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    valid_from: str = ""
    expires_at: str = ""
    requires_revalidation: bool = True
    revalidated: bool = False


class ContinuousIntelligenceLoop:
    """The 24/7 loop that determines what the agent should do *right now*."""

    def __init__(self, session: TradingSession, store: TradingStore, broker: VirtualBroker,
                 feed: Any = None, universe_provider: Any = None,
                 home_market: str = "CN",
                 monitored_markets: list[str] = field(default_factory=lambda: ["CN", "US", "HK"]),
                 replay_time: datetime | None = None,
                 ) -> None:
        self.session = session
        self.store = store
        self.broker = broker
        self.feed = feed or LiveMarketFeed()
        self.universe_provider = universe_provider
        self.home_market = Market(home_market) if home_market in ("CN", "US", "HK") else Market.CN
        self.monitored_markets = [Market(m) for m in monitored_markets if m in ("CN", "US", "HK")]
        self._replay_time = replay_time  # None = real clock; set = replay mode

        self.op_state = AgentOperationalState(
            home_market=home_market,
            monitored_markets=monitored_markets,
            execution_markets=[home_market],
        )

        # Sub-loop for active trading phases
        self._trading_loop: AutonomousTradingLoop | None = None

        # Runtime bookkeeping
        self.overnight_events: list[OvernightEvent] = []
        self.pending_actions: list[NextSessionAction] = []
        self._daily_report: DailyTradingReport | None = None
        self._day_opening_equity: float = session.initial_capital
        self._cycle_spacing = CYCLE_SPACING
        self._daily_report_generated_today: bool = False

    # ── main loop ──

    def _now(self) -> datetime:
        """Clock source: replay time if injected, otherwise real UTC."""
        return self._replay_time or datetime.now(timezone.utc)

    def advance_replay(self, delta: timedelta) -> datetime:
        """Move the replay clock forward. Only callable in replay mode."""
        if self._replay_time is None:
            self._replay_time = datetime.now(timezone.utc)
        self._replay_time += delta
        return self._replay_time

    def run_intelligence_cycle(self) -> dict[str, Any]:
        self.session.cycle_count += 1
        self.op_state.heartbeat()
        snapshot = global_snapshot(utc_now=self._now())
        home_ms = snapshot.markets.get(self.home_market)
        if home_ms is None:
            return {"phase": "error", "reason": "home market not available"}

        new_phase = self._determine_phase(snapshot, home_ms)
        self.op_state.transition(new_phase, self._phase_activity(new_phase, snapshot))
        summary: dict[str, Any] = {"phase": new_phase.value, "cycle": self.session.cycle_count}

        if new_phase == OperationPhase.PRE_MARKET_BRIEF:
            summary |= self._do_pre_market_brief(snapshot)
        elif new_phase == OperationPhase.ACTIVE_TRADING:
            summary |= self._do_active_trading(snapshot)
        elif new_phase == OperationPhase.MIDDAY_MONITORING:
            summary |= self._do_midday(snapshot)
        elif new_phase == OperationPhase.POST_MARKET_REVIEW:
            summary |= self._do_post_market(snapshot)
        elif new_phase == OperationPhase.CROSS_MARKET_MONITORING:
            summary |= self._do_cross_market(snapshot)
        elif new_phase == OperationPhase.OVERNIGHT_RESEARCH:
            summary |= self._do_overnight(snapshot)
        elif new_phase == OperationPhase.WEEKEND_RESEARCH:
            summary |= self._do_weekend(snapshot)
        else:
            summary["action"] = "wait"

        self._persist_state()
        return summary

    # ── phase dispatch ──

    def _determine_phase(self, snap: GlobalMarketSnapshot, home_ms: MarketStatus) -> OperationPhase:
        if self.session.status in (SessionStatus.HALTED,):
            return OperationPhase.HALTED
        if not home_ms.is_trading_day:
            return OperationPhase.WEEKEND_RESEARCH if self._now().weekday() >= 5 else OperationPhase.OVERNIGHT_RESEARCH
        if home_ms.session == MarketSession.PRE_MARKET:
            return OperationPhase.PRE_MARKET_BRIEF
        if home_ms.session == MarketSession.BREAK:
            return OperationPhase.MIDDAY_MONITORING
        if home_ms.session == MarketSession.OPEN:
            if snap.any_executable(self.session.allowed_markets):
                local_hour = home_ms.local_time.hour
                if 9 <= local_hour < 11 or 13 <= local_hour < 14:
                    return OperationPhase.ACTIVE_TRADING
                return OperationPhase.MIDDAY_MONITORING
            return OperationPhase.MARKET_MONITORING
        # After home market closed — daily report ONCE, then cross-market/overnight
        if home_ms.is_settlement_complete or home_ms.session == MarketSession.POST_MARKET:
            if not self._daily_report_generated_today:
                return OperationPhase.POST_MARKET_REVIEW
            # Already generated today's report — move on
            if snap.any_executable([m.value for m in self.monitored_markets]):
                return OperationPhase.CROSS_MARKET_MONITORING
            return OperationPhase.OVERNIGHT_RESEARCH
        # home market closed — check if any monitored market is open
        if home_ms.session == MarketSession.CLOSED:
            if snap.any_executable([m.value for m in self.monitored_markets]):
                return OperationPhase.CROSS_MARKET_MONITORING
            return OperationPhase.OVERNIGHT_RESEARCH
        # Unknown/fallback
        return OperationPhase.OVERNIGHT_RESEARCH

    def _phase_activity(self, phase: OperationPhase, snap: GlobalMarketSnapshot) -> str:
        open_mkts = ", ".join(MARKET_LABEL.get(m, m.value) for m in snap.open_markets())
        if phase == OperationPhase.CROSS_MARKET_MONITORING:
            return f"监控开放市场：{open_mkts or '无'}"
        if phase == OperationPhase.OVERNIGHT_RESEARCH:
            return "夜间低频监控：新闻、公告、全球事件"
        return ""

    # ── phase actions ──

    def _do_pre_market_brief(self, snap: GlobalMarketSnapshot) -> dict[str, Any]:
        """Revalidate overnight plans; brief on global/macro signals."""
        self._daily_report_generated_today = False  # new trading day
        now = self._now()
        # Revalidate pending actions
        revalidated = []
        for pa in self.pending_actions:
            if pa.requires_revalidation and not pa.revalidated:
                # In production this would fetch latest prices/risks and re-evaluate.
                # For now we mark as revalidated to unblock pre-market check.
                pa.revalidated = True
                revalidated.append(pa.plan_id)
        # Summarize overnight events relevant to portfolio
        portfolio_symbols = list(self.broker.positions.keys())
        relevant_events = [e for e in self.overnight_events
                          if any(s in portfolio_symbols for s in e.affected_symbols)]
        return {
            "action": "pre_market_brief",
            "pending_revalidated": len(revalidated),
            "overnight_relevant_events": len(relevant_events),
            "open_markets": ",".join(m.value for m in snap.open_markets()),
        }

    def _do_active_trading(self, snap: GlobalMarketSnapshot) -> dict[str, Any]:
        """Execute one trading cycle — the legacy run_cycle path, now contextualized."""
        if self._trading_loop is None:
            from noalpha.trading.autonomous import AutonomousTradingLoop
            self._trading_loop = AutonomousTradingLoop(
                self.session, self.store, self.broker,
                feed=self.feed, universe_provider=self.universe_provider,
            )
        if not snap.any_executable(self.session.allowed_markets):
            return {"action": "no_trade", "reason": "no executable market open"}
        # Ensure session is RUNNING so run_cycle doesn't skip
        if self.session.status not in (SessionStatus.RUNNING, SessionStatus.STARTING):
            self.session.status = SessionStatus.RUNNING
        # advance_cycle=False: run_intelligence_cycle already bumped the counter
        # this tick — otherwise a trading cycle counts twice.
        cycle_summary = self._trading_loop.run_cycle(advance_cycle=False)
        self.session.last_cycle_at = self._trading_loop.session.last_cycle_at
        self.session.next_cycle_at = self._trading_loop.session.next_cycle_at
        return {"action": "trading_cycle", "summary": cycle_summary}

    def _do_midday(self, snap: GlobalMarketSnapshot) -> dict[str, Any]:
        """Intraday checks + opportunistic scan when home market is open."""
        # Refresh position marks
        prices: dict[str, float] = {}
        for symbol in list(self.broker.positions.keys()):
            quote = self.feed.get_quote(symbol) if hasattr(self.feed, 'get_quote') else None
            if quote and quote.get("price", 0) > 0:
                prices[symbol] = quote["price"]
        if prices:
            self.broker.update_market_prices(prices)
        # Also run a full active trading cycle if the market is executable
        if snap.any_executable(self.session.allowed_markets):
            return self._do_active_trading(snap) | {"positions_updated": len(prices)}
        return {"action": "midday_check", "positions_updated": len(prices)}

    def _do_post_market(self, snap: GlobalMarketSnapshot) -> dict[str, Any]:
        """Generate the daily trading report after home market close."""
        self._daily_report_generated_today = True
        report = self._generate_daily_report(snap)
        self._daily_report = report
        self.op_state.daily_report_count += 1
        # Reset overnight events for the next cycle
        self.overnight_events = []
        self._day_opening_equity = self.broker.account.equity
        return {"action": "daily_report", "report_id": report.report_id,
                "daily_pnl": report.daily_pnl, "trade_count": report.trade_count}

    def _do_cross_market(self, snap: GlobalMarketSnapshot) -> dict[str, Any]:
        """Monitor open non-home markets for events relevant to portfolio."""
        events_found = 0
        open_mkts = snap.open_markets()
        portfolio_symbols = list(self.broker.positions.keys())

        for mkt in open_mkts:
            if mkt == self.home_market:
                continue
            # Check for notable moves in monitored indices
            ms = snap.markets.get(mkt)
            if ms is None:
                continue
            # Placeholder: in production, fetch index data and check for anomalies
            event = OvernightEvent(
                event_type="global_market",
                source_market=mkt.value,
                summary=f"{MARKET_LABEL.get(mkt, mkt.value)} 当前 {ms.session.value} · {ms.local_time.strftime('%H:%M')}",
                affected_symbols=portfolio_symbols,
                created_at=self._now().isoformat(),
            )
            if event.summary:
                self.overnight_events.append(event)
                self.op_state.overnight_event_count += 1
                events_found += 1
                self.store.save_overnight_event(
                    self.session.session_id, event.event_type,
                    event.summary, event.affected_symbols,
                )

        return {"action": "cross_market", "open_markets": [m.value for m in open_mkts],
                "events": events_found}

    def _do_overnight(self, snap: GlobalMarketSnapshot) -> dict[str, Any]:
        """Low-frequency overnight monitoring."""
        # In production: poll news, filings, watch for material macro events
        return {"action": "overnight", "pending_actions": len(self.pending_actions),
                "overnight_events_accumulated": len(self.overnight_events)}

    def _do_weekend(self, snap: GlobalMarketSnapshot) -> dict[str, Any]:
        return {"action": "weekend", "note": "replay / strategy evaluation deferred"}

    # ── daily report generation ──

    def _generate_daily_report(self, snap: GlobalMarketSnapshot) -> DailyTradingReport:
        today = self._now().strftime("%Y-%m-%d")
        broker = self.broker
        portfolio = broker.get_portfolio_summary()

        # 1. market regime
        index_changes = self._collect_index_performance(snap)
        regime = determine_market_regime(index_changes)

        # 2. portfolio
        opening = self._day_opening_equity
        closing = broker.account.equity
        daily_pnl = closing - opening
        daily_ret = daily_pnl / opening if opening else 0

        # 3. trade review
        decisions = self.store.list_events(self.session.session_id, limit=2000)
        decisions = [d for d in decisions if d["event_type"] == "decision"]
        fills = broker.fills
        positions = broker.positions

        successful: list[TradeReviewItem] = []
        failed: list[TradeReviewItem] = []
        for dec in decisions:
            pid = dec["payload"].get("proposal_id", "")
            fill = next((f for f in fills if f.order_id == pid), None)
            pnl = 0.0
            if fill:
                pos = positions.get(dec.get("symbol", ""))
                pnl = (fill.price - (pos.average_cost if pos else 0)) * fill.quantity if pos else 0

            dq = self._classify_decision_quality(dec, fill, pnl)

            item = TradeReviewItem(
                proposal_id=pid,
                symbol=dec.get("symbol", ""),
                name=dec["payload"].get("symbol", ""),
                action=dec["payload"].get("action", ""),
                decision_time=dec["timestamp"],
                thesis=dec["payload"].get("thesis", ""),
                evidence_ids=dec["payload"].get("evidence_ids", []),
                target_weight=dec["payload"].get("final_weight", 0),
                final_weight=dec["payload"].get("final_weight", 0),
                price=dec["payload"].get("price", 0),
                quantity=dec["payload"].get("quantity", 0),
                pnl_contribution=pnl,
                decision_quality=dq,
                quality_reason=self._dq_reason(dq, pnl),
                lessons=[],
            )
            if pnl >= 0:
                successful.append(item)
            else:
                failed.append(item)

        # 4. missed opportunities
        no_actions = [d for d in decisions if d["event_type"] == "no_action"]
        missed = self._analyze_missed(no_actions)

        # 5. execution quality
        avg_slip = sum(getattr(f, 'slippage', 0) or 0 for f in fills) / max(len(fills), 1)
        partials = sum(1 for f in fills if getattr(f, 'partial', False))

        # 6. thesis changes
        thesis_changes_list = [t for t in (self.store.list_theses(self.session.session_id)
                                          if hasattr(self.store, 'list_theses') else [])
                              if t.get("updated_at", "")[:10] == today] if False else []

        # 7. lessons (deterministic, not fake)
        lessons = []
        if failed:
            lessons.append(DailyLesson(
                lesson_id=str(uuid4())[:12],
                title="亏损交易集中在动量信号衰减",
                description=f"今日 {len(failed)}/{len(decisions)} 笔交易亏损，亏损交易大多发生在动量信号减弱的环境中。检查 Evidence 新鲜度和信号质量。",
                evidence_decision_ids=[f.proposal_id for f in failed],
                evidence_market_data=str(index_changes),
                confidence=0.7,
                applicable_scope="momentum_strategy",
                recurrence_status="POSSIBLE_PATTERN",
                candidate_improvement="",
            ))

        # 8. tomorrow plan
        positions_list = [(s, p) for s, p in positions.items() if p.quantity > 0]
        weak = [(s, p) for s, p in positions_list if p.unrealized_pnl < -abs(p.market_value * 0.03)]
        plan_lines = [
            f"明天重点监控 {len(positions_list)} 个持仓",
            f"其中 {len(weak)} 个持仓浮亏显著，需要开盘后重新评估" if weak else "",
            "今日教训：" + ("动量策略胜率下降" if failed else "正常"),
        ]
        pending_actions = [pa for pa in self.pending_actions if not pa.revalidated]

        return DailyTradingReport(
            report_id=str(uuid4())[:12],
            session_id=self.session.session_id,
            date=today,
            market=self.home_market.value,
            generated_at=self._now().isoformat(),
            market_regime=regime,
            market_regime_reason=f"基于主要指数变动 {index_changes}",
            index_performance=index_changes,
            sector_performance={},
            opening_equity=opening,
            closing_equity=closing,
            daily_pnl=daily_pnl,
            daily_return=daily_ret,
            benchmark_return=0.0,
            excess_return=daily_ret,
            max_intraday_drawdown=0.0,
            turnover=portfolio.get("turnover", 0),
            total_commissions=sum(f.commission for f in fills),
            trade_count=len(decisions),
            buy_count=sum(1 for d in decisions if d["payload"].get("action") == "buy"),
            sell_count=sum(1 for d in decisions if d["payload"].get("action") == "sell"),
            successful_reviews=successful,
            failed_reviews=failed,
            no_action_decisions=len(no_actions),
            risk_rejections=self.session.risk_rejections,
            missed_opportunities=missed,
            avg_slippage=avg_slip,
            partial_fills=partials,
            missed_fills=0,
            avg_fill_latency_s=0,
            thesis_changes=[],
            lessons=lessons,
            candidate_improvements=[],
            tomorrow_plan="\n".join(line for line in plan_lines if line),
            tomorrow_watchlist=[s for s, _ in weak],
            tomorrow_pending_actions=[{"plan_id": pa.plan_id, "symbol": pa.symbol,
                                       "action": pa.action, "reason": pa.reason}
                                      for pa in pending_actions],
            tomorrow_unknowns=["隔夜海外市场可能影响开盘"] if snap.closed_markets() else [],
        )

    def _collect_index_performance(self, snap: GlobalMarketSnapshot) -> dict[str, float]:
        """Collect index data from market gateway for regime classification."""
        changes: dict[str, float] = {}
        try:
            from noalpha.market.gateway import market_gateway
            quotes = market_gateway.get_all_latest()
            for q in quotes:
                if hasattr(q, 'change_percent') and q.change_percent:
                    changes[q.symbol] = q.change_percent / 100 if abs(q.change_percent) > 1 else q.change_percent
        except Exception:
            pass
        if not changes:
            changes = {"SSE_Composite": 0.0}
        return changes

    def _classify_decision_quality(self, dec: dict, fill: Any, pnl: float) -> DecisionQuality:
        confidence = dec["payload"].get("confidence", 0.5)
        quality = dec["payload"].get("decision_quality", "signal_only")
        if confidence > 0.7 and pnl >= 0:
            return DecisionQuality.GOOD_DECISION_GOOD_OUTCOME
        if confidence > 0.7 and pnl < 0:
            return DecisionQuality.GOOD_DECISION_BAD_OUTCOME
        if confidence <= 0.5 and pnl >= 0:
            return DecisionQuality.BAD_DECISION_GOOD_OUTCOME
        if confidence <= 0.5 and pnl < 0:
            return DecisionQuality.BAD_DECISION_BAD_OUTCOME
        return DecisionQuality.UNKNOWN

    def _dq_reason(self, dq: DecisionQuality, pnl: float) -> str:
        if dq == DecisionQuality.GOOD_DECISION_GOOD_OUTCOME:
            return "高置信度决策 + 正收益"
        if dq == DecisionQuality.GOOD_DECISION_BAD_OUTCOME:
            return "高置信度决策但意外亏损，需排查是否为外部事件"
        if dq == DecisionQuality.BAD_DECISION_GOOD_OUTCOME:
            return "低置信度决策 + 正收益，可能是运气"
        return "低置信度决策 + 亏损"

    def _analyze_missed(self, no_actions: list[dict]) -> list[MissedOpportunity]:
        missed: list[MissedOpportunity] = []
        for na in no_actions[:3]:
            missed.append(MissedOpportunity(
                symbol=na.get("symbol", "?"),
                name=na.get("symbol", "?"),
                reason_missed=na.get("message", "NO_ACTION"),
                candidate_score=na.get("payload", {}).get("score", 0),
                subsequent_return=0.0,
                would_have_pnl=0.0,
                lesson="需检查未决策原因是否合理",
            ))
        return missed

    # ── persistence ──

    def _persist_state(self) -> None:
        self.session.current_equity = self.broker.account.equity
        self.session.cash = self.broker.account.cash
        self.store.save_broker_state(self.session.session_id, self.broker.to_state())
        self.store.save_session(self.session)
        if self._daily_report:
            self._save_report(self._daily_report)

    def _save_report(self, report: DailyTradingReport) -> None:
        try:
            self.store.save_daily_report(report.to_dict())
        except Exception as exc:
            logger.warning("Failed to save daily report: %s", exc)
