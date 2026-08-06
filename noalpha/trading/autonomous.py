"""Autonomous trading engine: the loop that actually observes, researches,
proposes, risk-checks, sizes and executes against the VirtualBroker.

Main path (every cycle):
  AutonomousTradingLoop.run_cycle
    → feed.get_quote / feed.get_daily_bars          (real market data)
    → DecisionEngine.scan_universe                  (stage 1: cheap scan)
    → DecisionEngine.research                       (stage 2: candidates only)
    → DecisionEngine.build_buy_proposal / manage_position
    → PreTradeRiskEngine.check                      (hard enforcement)
    → PositionSizingEngine.size + lot rules
    → VirtualBroker.submit_order → Fill → Portfolio
    → TradingStore (decision_events / equity_curve / broker_state)

In-loop research now uses a TradingAgents-style deterministic role layer
(analysts → bull/bear researchers → trader → risk guardians) before hard risk
and execution. Optional LLM research is fused as a separate evidence signal;
the LLM never places orders directly.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from noalpha.market.fx import get_rate
from noalpha.market.gateway import market_gateway
from noalpha.market.instruments import parse_secid
from noalpha.market.kline_service import KlineService
from noalpha.market.live_quote import Market
from noalpha.quant.domain import SignalDirection
from noalpha.quant.ensemble import EnsembleInput, SignalEnsemble
from noalpha.quant.inference import InferenceResult, QuantInferenceService
from noalpha.quant.llm_features import LLMFeatureStore
from noalpha.quant.portfolio import InverseVolatilityOptimizer
from noalpha.quant.registry import QuantModelRegistry
from noalpha.trading.research_fusion import (
    FusionPolicy, TradingResearchService, default_trading_research_service,
    snapshot_to_signal,
)
from noalpha.trading.shadow import DecisionDelta, ShadowArena
from noalpha.trading.domain import Order, OrderSide, OrderStatus, OrderType
from noalpha.trading.proposal import (
    DecisionMode, ExitReason, TradeAction, TradeProposal, TradeRationale,
)
from noalpha.trading.risk import PositionSizingEngine, PreTradeRiskEngine
from noalpha.trading.session import DecisionEvent, SessionStatus, TradingSession, TradingStore
from noalpha.trading.thesis import InvestmentThesis, ThesisStatus
from noalpha.trading.trading_agents import TradingAgentsOrchestrator, TradingAgentsReport
from noalpha.trading.virtual_broker import VirtualBroker

logger = logging.getLogger(__name__)

RISK_PROFILES: dict[str, dict[str, float]] = {
    "conservative": {"max_position_weight": 0.05, "max_daily_loss_pct": 0.02,
                     "buy_score_threshold": 0.75, "stop_loss": -0.05, "take_profit": 0.10,
                     "base_target_weight": 0.04},
    "balanced": {"max_position_weight": 0.10, "max_daily_loss_pct": 0.05,
                 "buy_score_threshold": 0.60, "stop_loss": -0.08, "take_profit": 0.15,
                 "base_target_weight": 0.06},
    "aggressive": {"max_position_weight": 0.15, "max_daily_loss_pct": 0.08,
                   "buy_score_threshold": 0.50, "stop_loss": -0.12, "take_profit": 0.25,
                   "base_target_weight": 0.08},
}

MAX_DEEP_RESEARCH_PER_CYCLE = 3

# A symbol that just produced a BUY proposal is not re-proposed for this many
# cycles — one position, one thesis; the thesis review path (manage_position)
# owns it afterwards. Prevents the每轮重复提案 spam observed in production.
PROPOSAL_COOLDOWN_CYCLES = 10


class MarketFeed(Protocol):
    def get_quote(self, secid: str) -> dict[str, Any] | None: ...
    def get_daily_bars(self, secid: str, limit: int = 60) -> list[dict[str, Any]]: ...


class LiveMarketFeed:
    """Quotes from the polling gateway, bars from the real kline chain."""

    # Quant features need up to 61 bars of lookback (return_60d etc.); always
    # fetching at least this much keeps one cache entry valid for both the
    # cheap scan (60) and quant inference (120). A cache entry also remembers
    # how many bars were REQUESTED — a short entry can never silently satisfy
    # a longer request (that bug starved quant inference: "no fresh features").
    MIN_FETCH = 120

    def __init__(self) -> None:
        self.klines = KlineService()
        # secid -> (fetched_at, requested_limit, bars)
        self._bar_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}

    def get_quote(self, secid: str) -> dict[str, Any] | None:
        quote = market_gateway.get_latest(secid)
        if quote is None:
            fetched = market_gateway.fetch_now([secid])
            quote = fetched[0] if fetched else None
        return quote.to_dict() if quote else None

    def get_daily_bars(self, secid: str, limit: int = 60) -> list[dict[str, Any]]:
        cached = self._bar_cache.get(secid)
        if cached and time.time() - cached[0] < 600 and (
                len(cached[2]) >= limit or cached[1] >= limit):
            return cached[2][-limit:]
        fetch = max(limit, self.MIN_FETCH)
        result = self.klines.get_klines(secid, "1d", fetch)
        bars = result.get("bars") or []
        if bars:
            self._bar_cache[secid] = (time.time(), fetch, bars)
        return bars[-limit:]


class ReplayMarketFeed:
    """Historical bars replayed with a moving as-of pointer.

    Only bars strictly at or before the current replay date are visible —
    future leakage is impossible by construction (asserted in tests)."""

    def __init__(self, history: dict[str, list[dict[str, Any]]]) -> None:
        # history: secid -> full list of daily bars (time ascending)
        self.history = history
        self.current_date: str = ""

    def set_date(self, date_str: str) -> None:
        self.current_date = date_str

    def visible_bars(self, secid: str) -> list[dict[str, Any]]:
        return [b for b in self.history.get(secid, []) if b["time"][:10] <= self.current_date]

    def get_quote(self, secid: str) -> dict[str, Any] | None:
        bars = self.visible_bars(secid)
        if not bars:
            return None
        last = bars[-1]
        prev_close = bars[-2]["close"] if len(bars) >= 2 else last["open"]
        sec = parse_secid(secid)
        return {
            "secid": secid,
            "symbol": sec.code if sec else secid,
            "name": secid,
            "market": sec.market.value if sec else "US",
            "currency": sec.currency if sec else "USD",
            "price": last["close"], "open": last["open"], "high": last["high"], "low": last["low"],
            "previous_close": prev_close,
            "change": last["close"] - prev_close,
            "change_percent": ((last["close"] - prev_close) / prev_close * 100) if prev_close else 0.0,
            "volume": last["volume"], "turnover": last.get("turnover", 0.0),
            "timestamp": last["time"], "freshness": "replay", "market_session": "open",
            "delay_seconds": 0, "source": "replay",
        }

    def get_daily_bars(self, secid: str, limit: int = 60) -> list[dict[str, Any]]:
        return self.visible_bars(secid)[-limit:]


# ── signals & deterministic research ──


def compute_signals(bars: list[dict[str, Any]], quote: dict[str, Any]) -> dict[str, float]:
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    out: dict[str, float] = {
        "day_change_pct": quote.get("change_percent", 0.0) or 0.0,
    }
    if len(closes) >= 6:
        out["mom_5d"] = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] else 0.0
    if len(closes) >= 21:
        out["mom_20d"] = (closes[-1] - closes[-21]) / closes[-21] if closes[-21] else 0.0
        ma20 = sum(closes[-20:]) / 20
        out["price_vs_ma20"] = (closes[-1] - ma20) / ma20 if ma20 else 0.0
    if len(volumes) >= 21 and volumes[-1] > 0:
        avg_vol = sum(volumes[-21:-1]) / 20
        out["volume_ratio"] = volumes[-1] / avg_vol if avg_vol > 0 else 0.0
    if len(closes) >= 2:
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
        if rets:
            mean = sum(rets) / len(rets)
            out["volatility_20d"] = math.sqrt(sum((r - mean) ** 2 for r in rets[-20:]) / max(len(rets[-20:]), 1))
    return out


def opportunity_score(signals: dict[str, float]) -> float:
    """0..1 momentum+volume score used by the cheap scan."""
    score = 0.5
    score += max(min(signals.get("mom_5d", 0.0) * 3.0, 0.2), -0.2)
    score += max(min(signals.get("mom_20d", 0.0) * 1.5, 0.15), -0.15)
    score += max(min(signals.get("price_vs_ma20", 0.0) * 2.0, 0.1), -0.1)
    vr = signals.get("volume_ratio", 0.0)
    if vr >= 2.0 and signals.get("day_change_pct", 0.0) > 0:
        score += 0.1
    elif vr >= 2.0 and signals.get("day_change_pct", 0.0) < 0:
        score -= 0.1
    return max(0.0, min(1.0, score))


@dataclass
class Candidate:
    secid: str
    symbol: str
    name: str
    score: float
    signals: dict[str, float]
    quote: dict[str, Any]
    reason: str = ""


@dataclass
class ResearchOutcome:
    thesis: str
    bull_case: list[str]
    bear_case: list[str]
    risks: list[str]
    invalidation_conditions: list[str]
    confidence: float
    evidence: list[dict[str, Any]]
    research_mode: str = "deterministic"
    agent_report: TradingAgentsReport | None = None

    def evidence_ids(self) -> list[str]:
        return [e["evidence_id"] for e in self.evidence]


class DecisionEngine:
    """Deterministic, evidence-based decision logic (stage 1 + stage 2)."""

    def __init__(self, profile: dict[str, float]) -> None:
        self.profile = profile
        self.agents = TradingAgentsOrchestrator(profile)

    def scan_universe(self, universe: list[dict[str, Any]], feed: MarketFeed) -> tuple[list[Candidate], list[str]]:
        candidates: list[Candidate] = []
        skipped: list[str] = []
        for item in universe:
            secid = item["secid"]
            quote = feed.get_quote(secid)
            if quote is None or quote.get("price", 0) <= 0:
                skipped.append(f"{secid}: no quote")
                continue
            bars = feed.get_daily_bars(secid, 60)
            if len(bars) < 10:
                skipped.append(f"{secid}: insufficient history ({len(bars)} bars)")
                continue
            signals = compute_signals(bars, quote)
            score = opportunity_score(signals)
            candidates.append(Candidate(
                secid=secid, symbol=quote.get("symbol", ""), name=quote.get("name", secid),
                score=score, signals=signals, quote=quote,
                reason=f"score={score:.2f} mom5={signals.get('mom_5d', 0):+.2%} "
                       f"volx={signals.get('volume_ratio', 0):.1f}",
            ))
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates, skipped

    def research(self, candidate: Candidate, feed: MarketFeed) -> ResearchOutcome:
        bars = feed.get_daily_bars(candidate.secid, 60)
        s = candidate.signals
        q = candidate.quote
        now = datetime.now(timezone.utc).isoformat()
        evidence: list[dict[str, Any]] = []

        def ev(kind: str, content: str, value: float) -> str:
            eid = f"EV-{candidate.symbol}-{kind}-{len(evidence)}"
            evidence.append({
                "evidence_id": eid, "evidence_type": "market_signal", "source": q.get("source", ""),
                "content": content, "value": value, "as_of": q.get("timestamp", now),
                "bars_used": len(bars),
            })
            return eid

        bull, bear, risks = [], [], []
        if s.get("mom_5d", 0) > 0.02:
            bull.append(f"5日动量 {s['mom_5d']:+.2%}（{ev('mom5', f'5-day momentum {s['mom_5d']:+.2%}', s['mom_5d'])}）")
        elif s.get("mom_5d", 0) < -0.02:
            bear.append(f"5日动量 {s['mom_5d']:+.2%}（{ev('mom5', f'5-day momentum {s['mom_5d']:+.2%}', s['mom_5d'])}）")
        if s.get("mom_20d", 0) > 0.05:
            bull.append(f"20日动量 {s['mom_20d']:+.2%}（{ev('mom20', f'20-day momentum {s['mom_20d']:+.2%}', s['mom_20d'])}）")
        elif s.get("mom_20d", 0) < -0.05:
            bear.append(f"20日动量 {s['mom_20d']:+.2%}（{ev('mom20', f'20-day momentum {s['mom_20d']:+.2%}', s['mom_20d'])}）")
        if s.get("volume_ratio", 0) >= 2.0:
            direction = "放量上行" if s.get("day_change_pct", 0) > 0 else "放量下行"
            (bull if s.get("day_change_pct", 0) > 0 else bear).append(
                f"{direction}，量比 {s['volume_ratio']:.1f}x（{ev('vol', f'volume ratio {s['volume_ratio']:.1f}x', s['volume_ratio'])}）")
        if s.get("price_vs_ma20") is not None:
            side = "上方" if s.get("price_vs_ma20", 0) >= 0 else "下方"
            (bull if s.get("price_vs_ma20", 0) >= 0 else bear).append(
                f"价格位于 20 日均线{side} {abs(s.get('price_vs_ma20', 0)):.2%}"
                f"（{ev('ma20', f'price vs MA20 {s.get('price_vs_ma20', 0):+.2%}', s.get('price_vs_ma20', 0))}）")
        vol = s.get("volatility_20d", 0)
        if vol > 0.03:
            risks.append(f"20日波动率偏高 {vol:.2%}")
        risks.append("动量策略在震荡市中失效风险")

        confidence = min(0.9, max(0.1, candidate.score * (0.85 if vol > 0.03 else 1.0)))
        thesis = (
            f"{candidate.name}({candidate.symbol}) 综合动量得分 {candidate.score:.2f}："
            f"5日 {s.get('mom_5d', 0):+.2%}、20日 {s.get('mom_20d', 0):+.2%}、"
            f"量比 {s.get('volume_ratio', 0):.1f}x。基于真实日K与实时报价的确定性信号研究"
            f"（非LLM，research_mode=deterministic）。"
        )
        invalidation = [
            "收盘价跌破20日均线且5日动量转负",
            f"持仓亏损超过止损线 {self.profile['stop_loss']:.0%}",
            "放量下跌（量比≥2且当日跌幅≥3%）",
        ]
        agent_report = self.agents.run(
            symbol=candidate.symbol,
            name=candidate.name,
            score=candidate.score,
            signals=candidate.signals,
            evidence_ids=[e["evidence_id"] for e in evidence],
        )
        if agent_report.trader_plan:
            confidence = min(confidence, max(0.1, agent_report.trader_plan.confidence))
        if agent_report.fund_manager.get("sizing_adjustment"):
            confidence = max(0.1, confidence * float(agent_report.fund_manager["sizing_adjustment"]))

        return ResearchOutcome(
            thesis=thesis, bull_case=bull, bear_case=bear, risks=risks,
            invalidation_conditions=invalidation, confidence=confidence, evidence=evidence,
            agent_report=agent_report,
        )

    def build_buy_proposal(self, candidate: Candidate, research: ResearchOutcome) -> TradeProposal:
        base = self.profile["base_target_weight"]
        raw = base * (0.5 + research.confidence)
        cap = self.profile["max_position_weight"]
        target = min(raw, cap)

        # ── explainability ──
        n_bull = len(research.bull_case)
        n_bear = len(research.bear_case)
        ranking_note = f"本轮 {len(research.evidence)} 条信号证据，{n_bull} 利多 {n_bear} 利空"

        agent_report = research.agent_report
        trader_plan = agent_report.trader_plan if agent_report else None
        debate = agent_report.debate if agent_report else None
        agent_summary = (
            f"TradingAgents 角色链路完成：多空共识 {debate.consensus}、"
            f"冲突 {debate.conflict_level:.2f}；交易员建议 {trader_plan.action_hint}。"
            if debate and trader_plan else
            "TradingAgents 角色链路不可用，回退确定性信号研究。"
        )
        decision_summary = (
            f"AI 选择买入 {candidate.name}({candidate.symbol})。"
            f"短中期动量信号共振——5日 {candidate.signals.get('mom_5d', 0):+.2%}、"
            f"20日 {candidate.signals.get('mom_20d', 0):+.2%}，"
            f"综合候选评分 {candidate.score:.2f}；"
            f"{agent_summary}"
            f" 当前未伪造基本面/新闻/情绪数据，最终仓位仍受外部硬风控约束。"
        )

        catalysts: list[dict[str, Any]] = []
        if candidate.signals.get("mom_5d", 0) > 0.02:
            catalysts.append({
                "title": "短期动量增强",
                "description": f"5日动量 {candidate.signals['mom_5d']:+.2%}，显著跑赢基准",
                "evidence_ids": research.evidence_ids()[:3],
                "expected_time_horizon": "5-15 trading days",
                "confidence": min(0.85, 0.55 + abs(candidate.signals["mom_5d"])),
            })
        if candidate.signals.get("mom_20d", 0) > 0.05:
            catalysts.append({
                "title": "中期趋势延续",
                "description": f"20日动量 {candidate.signals['mom_20d']:+.2%}，趋势同步确认",
                "evidence_ids": research.evidence_ids()[2:5],
                "expected_time_horizon": "15-40 trading days",
                "confidence": min(0.80, 0.55 + abs(candidate.signals["mom_20d"])),
            })

        weight_reason = {
            "base_weight": base,
            "confidence_multiplier": 0.5 + research.confidence,
            "raw_weight": round(raw, 4),
            "cap": cap,
            "final_weight": round(target, 4),
        }

        positive_factors = [
            f"5日动量 {candidate.signals.get('mom_5d', 0):+.2%}" if candidate.signals.get("mom_5d", 0) > 0.02 else None,
            f"20日动量 {candidate.signals.get('mom_20d', 0):+.2%}" if candidate.signals.get("mom_20d", 0) > 0.05 else None,
            f"候选评分 {candidate.score:.2f} 超过阈值 {self.profile['buy_score_threshold']:.2f}",
        ]
        negative_factors = [
            "当前无基本面催化证据（技术信号策略固有局限）",
            f"波动率 {candidate.signals.get('volatility_20d', 0):.2%}" if candidate.signals.get("volatility_20d", 0) > 0.03 else None,
            "缺乏跨来源交叉验证（仅有行情数据）",
            n_bear > 0 and f"存在 {n_bear} 条空头信号" or None,
        ]

        return TradeProposal(
            symbol=candidate.symbol,
            action=TradeAction.BUY,
            target_weight=round(target, 4),
            rationale=TradeRationale(
                thesis=research.thesis,
                signal_reason=(
                    f"综合动量得分 {candidate.score:.2f}："
                    f"5日 {candidate.signals.get('mom_5d', 0):+.2%}、"
                    f"20日 {candidate.signals.get('mom_20d', 0):+.2%}、"
                    f"量比 {candidate.signals.get('volume_ratio', 0):.1f}x"
                ),
                decision_summary=decision_summary,
                why_this_instrument=(
                    f"{candidate.name}({candidate.symbol}) 在本轮扫描中排名靠前"
                    f"（评分 {candidate.score:.2f}，阈值 {self.profile['buy_score_threshold']:.2f}）。"
                    f"{ranking_note}。"
                    f"当前无持仓冲突，满足 TradingAgents 交易员准入和外部风控前置条件。"
                ),
                why_now=(trader_plan.why_now if trader_plan else [c.reason for c in [candidate] if c.reason]),
                catalysts=catalysts,
                supporting_evidence_ids=research.evidence_ids(),
                counter_arguments=research.bear_case,
                risks=research.risks,
                invalidation_conditions=(trader_plan.invalidation_conditions if trader_plan else research.invalidation_conditions),
                confidence=research.confidence,
                target_weight_reason=weight_reason,
                confidence_reason={
                    "positive": [p for p in positive_factors if p],
                    "negative": [n for n in negative_factors if n],
                },
                agent_view=agent_report.to_dict() if agent_report else {},
            ),
            confidence=research.confidence,
            decision_quality="signal_only",
        )

    def manage_position(self, symbol: str, position: Any, quote: dict[str, Any],
                        signals: dict[str, float], thesis: InvestmentThesis | None) -> tuple[TradeProposal | None, str]:
        """Position management: HOLD / REDUCE / SELL with explicit exit reasons."""
        if position.quantity <= 0 or position.average_cost <= 0:
            return None, "no position"
        price = quote.get("price", 0.0)
        if price <= 0:
            return None, "no price"
        ret = (price - position.average_cost) / position.average_cost

        def exit_proposal(action: TradeAction, weight: float, exit_reason: ExitReason, why: str,
                          confidence: float) -> TradeProposal:
            return TradeProposal(
                symbol=symbol, action=action, target_weight=weight,
                rationale=TradeRationale(
                    thesis=why, confidence=confidence,
                    invalidation_conditions=[],
                    risks=[],
                ),
                confidence=confidence,
                risk_result={"exit_reason": exit_reason.value},
            )

        if ret <= self.profile["stop_loss"]:
            return exit_proposal(
                TradeAction.CLOSE, 0.0, ExitReason.STOP_LOSS,
                f"止损触发：持仓收益 {ret:+.2%} ≤ 止损线 {self.profile['stop_loss']:.0%}", 0.9,
            ), "stop_loss"
        if ret >= self.profile["take_profit"]:
            return exit_proposal(
                TradeAction.CLOSE, 0.0, ExitReason.TAKE_PROFIT,
                f"止盈触发：持仓收益 {ret:+.2%} ≥ 止盈线 {self.profile['take_profit']:.0%}", 0.85,
            ), "take_profit"
        # New negative evidence: heavy-volume selloff
        if signals.get("volume_ratio", 0) >= 2.0 and signals.get("day_change_pct", 0) <= -3.0:
            return exit_proposal(
                TradeAction.REDUCE, 0.5, ExitReason.NEW_NEGATIVE_EVIDENCE,
                f"新负面证据：放量下跌（量比 {signals.get('volume_ratio', 0):.1f}x，"
                f"当日 {signals.get('day_change_pct', 0):+.1f}%），减仓一半", 0.75,
            ), "negative_evidence"
        # Thesis invalidation: momentum thesis broken
        if signals.get("price_vs_ma20", 0) < -0.02 and signals.get("mom_5d", 0) < -0.03:
            return exit_proposal(
                TradeAction.CLOSE, 0.0, ExitReason.THESIS_INVALIDATED,
                f"论点失效：价格跌破20日均线 {signals.get('price_vs_ma20', 0):+.2%} "
                f"且5日动量 {signals.get('mom_5d', 0):+.2%} 转负，动量论点不再成立", 0.8,
            ), "thesis_invalidated"
        hold_reason = (
            f"{symbol} 继续持有：收益率 {ret:+.2%}，"
            f"未触发止损 {self.profile['stop_loss']:.0%}/止盈 {self.profile['take_profit']:.0%}，"
            f"动量论点未失效且无新负面证据。"
        ) if ret is not None else f"{symbol} 持仓维持，当前无卖出信号。"
        return None, hold_reason


# ── the loop ──


class AutonomousTradingLoop:
    def __init__(self, session: TradingSession, store: TradingStore, broker: VirtualBroker,
                 feed: MarketFeed | None = None,
                 universe_provider: Any = None) -> None:
        self.session = session
        self.store = store
        self.broker = broker
        self.feed = feed or LiveMarketFeed()
        self.universe_provider = universe_provider  # callable -> list[{"secid","symbol",...}]
        profile = dict(RISK_PROFILES.get(session.risk_profile, RISK_PROFILES["balanced"]))
        profile["max_position_weight"] = session.max_position_weight or profile["max_position_weight"]
        profile["max_daily_loss_pct"] = session.max_daily_loss_pct or profile["max_daily_loss_pct"]
        self.profile = profile
        self.engine = DecisionEngine(profile)
        self.risk = PreTradeRiskEngine(
            max_position_weight=profile["max_position_weight"],
            max_daily_loss_pct=profile["max_daily_loss_pct"],
        )
        self.sizing = PositionSizingEngine(max_position_weight=profile["max_position_weight"])
        self.theses: dict[str, InvestmentThesis] = {}
        self._secid_by_symbol: dict[str, str] = {}
        # ── Quantitative Intelligence Layer (Phase Q) ──
        self.quant = QuantInferenceService(QuantModelRegistry())
        self.ensemble = SignalEnsemble()
        self.portfolio_optimizer = InverseVolatilityOptimizer(
            max_weight=profile["max_position_weight"], turnover_penalty=0.25)
        self.quant_result: InferenceResult | None = None
        # ── Phase Q5: real LLM research fusion + forward feature accumulation ──
        self.llm_feature_store = LLMFeatureStore()
        self.fusion_policy = FusionPolicy()
        self.trading_research: TradingResearchService | None = None
        self.llm_init_error = ""
        if session.research_mode == "llm":
            try:
                self.trading_research = default_trading_research_service(
                    feature_store=self.llm_feature_store)
                if self.trading_research is None:
                    self.llm_init_error = "no LLM API key configured"
            except Exception as exc:
                self.llm_init_error = str(exc)
                logger.exception("trading research service unavailable")
        self.shadow = ShadowArena(session.session_id, session.initial_capital)
        # ── system truth: what the loop is doing / which brains are real ──
        self._proposal_cooldown: dict[str, int] = {}   # symbol -> cycle of last BUY proposal
        self.current_stage: str = "idle"               # scan/quant/positions/research/…
        self.current_target: str = ""                  # symbol under research
        self.last_cycle_summary: dict[str, Any] = {}
        self.effective_mode: str = self._compute_mode(quant_ok=False)
        self._load_theses()

    def _load_theses(self) -> None:
        """Rehydrate persisted theses so restarts keep position→thesis links."""
        try:
            rows = self.store.list_theses(self.session.session_id)
        except Exception:
            return
        import json as _json
        for r in rows:
            try:
                thesis = InvestmentThesis(
                    thesis_id=r["thesis_id"], symbol=r["symbol"],
                    status=ThesisStatus(r["status"]),
                    thesis_text=r["thesis_text"] or "",
                    supporting_evidence_ids=_json.loads(r["supporting_evidence_ids"] or "[]"),
                    risks=_json.loads(r["risks"] or "[]"),
                    invalidation_conditions=_json.loads(r["invalidation_conditions"] or "[]"),
                    expected_horizon=r["expected_horizon"] or "20-60 trading days",
                    confidence=r["confidence"] or 0.0,
                    proposal_id=r["proposal_id"] or "",
                    created_at=r["created_at"] or "", updated_at=r["updated_at"] or "",
                    closed_at=r["closed_at"] or "",
                )
                self.theses[thesis.thesis_id] = thesis
            except Exception:
                logger.warning("skipping malformed thesis row %s", r.get("thesis_id"))

    def _save_thesis(self, thesis: InvestmentThesis) -> None:
        try:
            self.store.save_thesis(thesis, self.session.session_id)
        except Exception:
            logger.exception("thesis persistence failed")

    def _compute_mode(self, quant_ok: bool) -> str:
        """The REAL decision mode for this cycle — from actual component state,
        not from configuration wishes."""
        llm_requested = self.session.research_mode == "llm"
        llm_active = self.trading_research is not None
        if quant_ok and llm_active:
            return DecisionMode.QUANT_LLM_HYBRID.value
        if quant_ok and llm_requested and not llm_active:
            return DecisionMode.QUANT_ONLY_FALLBACK.value
        if quant_ok:
            return DecisionMode.QUANT_DETERMINISTIC.value
        if llm_active:
            return DecisionMode.LLM_RESEARCH.value
        if llm_requested:
            return DecisionMode.DETERMINISTIC_FALLBACK.value
        return DecisionMode.DETERMINISTIC_SIGNAL.value

    # ── event helper ──

    def _emit(self, event_type: str, message: str, secid: str = "", symbol: str = "",
              payload: dict[str, Any] | None = None) -> None:
        self.store.add_event(DecisionEvent(
            session_id=self.session.session_id, cycle=self.session.cycle_count,
            event_type=event_type, secid=secid, symbol=symbol,
            message=message, payload=payload or {},
        ))

    def _universe(self) -> list[dict[str, Any]]:
        if self.universe_provider is not None:
            items = self.universe_provider()
        else:
            items = []
        allowed = set(self.session.allowed_markets)
        return [i for i in items if i.get("market", "US") in allowed]

    def _account_price(self, quote: dict[str, Any]) -> tuple[float, dict[str, Any] | None]:
        """Instrument price converted into the session account currency using
        a real FX quote. Returns (price, fx_info); price 0 when FX missing."""
        price = quote.get("price", 0.0)
        ccy = (quote.get("currency") or self.session.currency).upper()
        if ccy == self.session.currency.upper():
            return price, None
        fx = get_rate(ccy, self.session.currency)
        if fx is None:
            return 0.0, None
        return price * fx["rate"], fx

    def _tradable(self, quote: dict[str, Any]) -> tuple[bool, str]:
        freshness = quote.get("freshness", "unknown")
        if freshness in ("live", "delayed", "replay"):
            return True, freshness
        if freshness == "closed" and self.session.allow_closed_market_fills:
            return True, freshness
        return False, freshness

    # ── main cycle ──

    def run_cycle(self, advance_cycle: bool = True) -> dict[str, Any]:
        """One trading cycle. advance_cycle=False when an outer loop (the
        ContinuousIntelligenceLoop) already owns the cycle counter — otherwise
        every trading cycle would be counted twice."""
        session = self.session
        if session.status not in (SessionStatus.RUNNING, SessionStatus.STARTING):
            return {"skipped": True, "reason": f"session status {session.status.value}"}
        if advance_cycle:
            session.cycle_count += 1
        session.last_cycle_at = datetime.now(timezone.utc).isoformat()
        cycle = session.cycle_count
        if self.trading_research is not None:
            self.trading_research.budget.new_cycle()
        summary: dict[str, Any] = {"cycle": cycle, "actions": [], "no_actions": []}

        universe = self._universe()
        self.current_stage = "scan"
        self.current_target = ""
        self._emit("scan", f"扫描 {len(universe)} 只标的（universe={session.universe}，"
                           f"markets={','.join(session.allowed_markets)}）",
                   payload={"universe_size": len(universe)})

        if not universe:
            self._emit("no_action", "候选池为空：watchlist 中没有属于允许市场的启用标的",
                       payload={"reason": "empty_universe"})
            summary["no_actions"].append("empty_universe")
            self._finish_cycle(summary)
            return summary

        # Stage 1: cheap scan
        candidates, skipped = self.engine.scan_universe(universe, self.feed)
        for msg in skipped:
            self._emit("scan", f"跳过 {msg}", payload={"skipped": msg})
        for c in candidates:
            self._secid_by_symbol[c.symbol] = c.secid

        # Stage 1.5: quant inference — cross-sectional ranking over the universe.
        # When an ACTIVE model exists, its ranking (not the heuristic score)
        # decides which candidates deserve deep research.
        market_scope = (self.session.allowed_markets[0]
                        if self.session.allowed_markets else "ALL")
        self.current_stage = "quant_inference"
        quant = self.quant.infer(universe, self.feed, market_scope)
        self.quant_result = quant
        # Persist the full inference so the Quant panel stays consistent with
        # System Truth even for a restored loop (no "no inference has run yet"
        # while the truth bar says HEALTHY).
        try:
            self._emit("quant_full", "量化推理快照", payload=quant.to_dict())
        except Exception:
            logger.exception("quant snapshot persist failed")
        signal_by_symbol = {s.symbol: s for s in quant.signals}
        quant_ok = not quant.error and bool(quant.predictions)
        self.effective_mode = self._compute_mode(quant_ok=quant_ok)
        summary["effective_mode"] = self.effective_mode
        self._emit("mode", f"本轮有效决策模式：{self.effective_mode}",
                   payload={"effective_mode": self.effective_mode,
                            "quant_available": quant_ok,
                            "quant_error": quant.error,
                            "llm_requested": session.research_mode == "llm",
                            "llm_active": self.trading_research is not None,
                            "llm_init_error": self.llm_init_error})
        if quant.error:
            self._emit("quant", f"量化推理不可用：{quant.error}（回退启发式评分）",
                       payload={"error": quant.error, "skipped": quant.skipped[:10]})
        else:
            top = [{"symbol": p.symbol, "rank": p.predicted_rank,
                    "predicted_return": round(p.predicted_return or 0.0, 5),
                    "probability_up": p.probability_up}
                   for p in quant.predictions[:5]]
            self._emit("quant", f"量化推理完成：{quant.model.get('display')} 对 "
                                f"{len(quant.predictions)} 只标的横截面排名"
                                f"（horizon {quant.model.get('horizon_days')}d）",
                       payload={"model": quant.model, "top_predictions": top,
                                "universe_size": len(quant.predictions),
                                "skipped": quant.skipped[:10],
                                "as_of": quant.as_of})
            # rank order replaces the heuristic order; heuristic kept as tiebreak
            strength = {s.symbol: s.strength for s in quant.signals}
            candidates.sort(key=lambda c: (strength.get(c.symbol, 0.0), c.score), reverse=True)

        # Position management first (existing holdings get checked every cycle)
        self.current_stage = "position_review"
        acted_symbols: set[str] = set()
        for symbol, position in list(self.broker.positions.items()):
            if position.quantity <= 0:
                continue
            cand = next((c for c in candidates if c.symbol == symbol), None)
            quote = cand.quote if cand else (self.feed.get_quote(self._secid_by_symbol.get(symbol, symbol)) or {})
            signals = cand.signals if cand else {}
            acct_price, _fx = self._account_price(quote)
            if acct_price > 0:
                self.broker.update_market_prices({symbol: acct_price})
            thesis = self.theses.get(position.active_thesis_id)
            proposal, verdict = self.engine.manage_position(symbol, position, quote, signals, thesis)
            # Quant-driven exit (第八十七/八十八条): rule exits (stop loss etc.)
            # keep priority; if none fired, a position whose cross-sectional
            # rank collapsed into the bottom third with negative expected
            # return is reduced — selling is not only stop-loss driven.
            qsig = signal_by_symbol.get(symbol)
            if proposal is None and qsig is not None and qsig.direction == SignalDirection.SHORT:
                pred = next((p for p in (self.quant_result.predictions if self.quant_result else [])
                             if p.symbol == symbol), None)
                weight = (position.market_value / self.broker.account.equity
                          if self.broker.account.equity > 0 else 0.0)
                if pred is not None and (pred.predicted_return or 0.0) < 0:
                    # below 1.5% weight a repeated half-reduce just churns
                    # commissions — close the remnant instead
                    close_out = weight < 0.015
                    proposal = TradeProposal(
                        symbol=symbol,
                        action=TradeAction.CLOSE if close_out else TradeAction.REDUCE,
                        target_weight=0.0 if close_out else 0.5,
                        rationale=TradeRationale(
                            thesis=(f"量化排名恶化：{symbol} 当前横截面排名 "
                                    f"{pred.predicted_rank}/{pred.universe_size}（后1/3），"
                                    f"预期{pred.horizon_days}日超额收益转负 "
                                    f"{pred.predicted_return:+.2%}，减仓一半"),
                            confidence=qsig.confidence,
                            quant_view={"rank": pred.predicted_rank,
                                        "universe_size": pred.universe_size,
                                        "predicted_return": pred.predicted_return,
                                        "model": qsig.model_version,
                                        "signal_direction": qsig.direction.value},
                        ),
                        confidence=qsig.confidence,
                        risk_result={"exit_reason": ExitReason.RANK_DETERIORATION.value},
                    )
                    proposal.decision_mode = self.effective_mode
                    verdict = "rank_deterioration"
            if proposal is not None and not proposal.decision_mode:
                proposal.decision_mode = DecisionMode.DETERMINISTIC_SIGNAL.value
            if proposal is None:
                position.hold_rationale = verdict  # persist why we hold
                self._emit("position", f"{symbol} 持仓检查：{verdict}", symbol=symbol,
                           payload={"verdict": verdict, "unrealized_pnl": position.unrealized_pnl,
                                    "quant_rank": qsig.strength if qsig else None})
                continue
            acted_symbols.add(symbol)
            self._execute_proposal(proposal, quote, summary, exit_verdict=verdict, thesis=thesis)

        # ── Stage 2: three-arm research + fusion (Phase Q5) ──
        # Arm A (quant_only):       quant signal alone
        # Arm B (deterministic_hybrid): quant + deterministic research ensemble (PRESERVED baseline)
        # Arm C (llm_hybrid):       quant + REAL LLM research fusion (new in Phase Q5)
        # Only one arm drives the real broker (session.research_mode); the
        # other two run as weight-space shadow portfolios (第四十四条).
        threshold = self.profile["buy_score_threshold"]
        researched = 0
        buys = 0
        self.current_stage = "candidate_research"
        for cand in candidates:
            if researched >= MAX_DEEP_RESEARCH_PER_CYCLE and self.trading_research is None:
                break
            if cand.symbol in acted_symbols:
                continue
            if cand.score < threshold:
                continue
            # ── dedup / cooldown gates（第二十五条）──
            last_prop_cycle = self._proposal_cooldown.get(cand.symbol)
            if last_prop_cycle is not None and cycle - last_prop_cycle < PROPOSAL_COOLDOWN_CYCLES:
                summary["no_actions"].append(f"{cand.symbol}:cooldown")
                continue
            active = self.store.active_proposal(self.session.session_id, cand.symbol)
            if active is not None:
                summary["no_actions"].append(f"{cand.symbol}:active_proposal_exists")
                continue
            existing = self.broker.positions.get(cand.symbol)
            existing_weight = (existing.market_value / self.broker.account.equity) if existing and existing.quantity > 0 and self.broker.account.equity > 0 else 0.0
            if existing_weight >= self.profile["max_position_weight"] * 0.95:
                continue
            if existing is not None and existing.quantity > 0:
                # Adding to a position must be able to buy at least one lot;
                # otherwise every cycle re-proposes and dies at zero_quantity.
                sec = parse_secid(cand.secid)
                lot = 100 if sec and sec.market == Market.CN else 1
                headroom_notional = (self.profile["max_position_weight"] - existing_weight) \
                    * self.broker.account.equity
                acct_price, _ = self._account_price(cand.quote)
                if acct_price > 0 and headroom_notional < lot * acct_price:
                    self._emit("no_action", f"{cand.symbol} 已持仓 {existing_weight:.1%}，"
                                            f"剩余额度不足一手（{lot} 股），跳过加仓",
                               secid=cand.secid, symbol=cand.symbol,
                               payload={"reason": "position_near_cap",
                                        "existing_weight": existing_weight,
                                        "headroom_notional": round(headroom_notional, 2),
                                        "lot_notional": round(lot * acct_price, 2)})
                    summary["no_actions"].append(f"{cand.symbol}:position_near_cap")
                    continue
            tradable, freshness = self._tradable(cand.quote)
            if not tradable:
                self._emit("no_action", f"{cand.symbol} 达到候选阈值(score={cand.score:.2f})但市场不可交易"
                                        f"（freshness={freshness}）", secid=cand.secid, symbol=cand.symbol,
                           payload={"reason": "market_not_tradable", "freshness": freshness, "score": cand.score})
                summary["no_actions"].append(f"{cand.symbol}:market_not_tradable")
                continue
            self._emit("candidate", f"{cand.symbol} 入选研究候选：{cand.reason}",
                       secid=cand.secid, symbol=cand.symbol,
                       payload={"score": cand.score, "signals": cand.signals})
            researched += 1
            self.current_target = f"{cand.name}({cand.symbol})"

            # ── deterministic research (Arm B baseline, always computed) ──
            research = self.engine.research(cand, self.feed)
            self._emit("research", f"{cand.symbol} 研究完成：confidence={research.confidence:.2f}"
                                   f"（deterministic 信号研究）",
                       secid=cand.secid, symbol=cand.symbol,
                       payload={"thesis": research.thesis, "bull_case": research.bull_case,
                                "bear_case": research.bear_case, "risks": research.risks,
                                "evidence": research.evidence, "confidence": research.confidence,
                                "research_mode": research.research_mode})
            if research.agent_report is not None:
                agent_payload = research.agent_report.to_dict()
                self._emit("agent", f"{cand.symbol} TradingAgents 角色链路完成："
                                    f"{agent_payload.get('debate', {}).get('consensus', 'neutral')} → "
                                    f"{agent_payload.get('trader_plan', {}).get('action_hint', 'no_action')}",
                           secid=cand.secid, symbol=cand.symbol,
                           payload=agent_payload)
            if research.confidence < 0.5:
                self._emit("no_action", f"{cand.symbol} 研究置信度 {research.confidence:.2f} < 0.5，放弃",
                           secid=cand.secid, symbol=cand.symbol,
                           payload={"reason": "low_confidence", "confidence": research.confidence})
                summary["no_actions"].append(f"{cand.symbol}:low_confidence")
                continue

            qsig = signal_by_symbol.get(cand.symbol)
            pred = next((p for p in (self.quant_result.predictions if self.quant_result else [])
                         if p.symbol == cand.symbol), None)

            # ── real LLM research (Arm C, Phase Q5) ──
            llm_snap, llm_sig, fusion = None, None, None
            if self.trading_research is not None:
                day_change = cand.signals.get("day_change_pct", 0.0)
                llm_snap, snap_status = self.trading_research.research(
                    secid=cand.secid, symbol=cand.symbol,
                    market=cand.quote.get("market", ""),
                    day_change_pct=day_change)
                self.shadow.llm_stats["calls"] += 1
                if llm_snap is not None:
                    llm_sig = snapshot_to_signal(llm_snap)
                    self.shadow.llm_stats["tokens"] += llm_snap.input_tokens + llm_snap.output_tokens
                    self.shadow.llm_stats["latency_ms_total"] += llm_snap.latency_ms
                else:
                    self.shadow.llm_stats["failures"] += 1
                # always call fusion (handles llm=None internally as fallback)
                fusion = self.fusion_policy.fuse(
                    symbol=cand.symbol, secid=cand.secid, quant=qsig, llm=llm_sig)
                if fusion.effective_mode != "hybrid_llm_quant":
                    self.shadow.llm_stats["fallback_cycles"] += 1
                if fusion.conflict_state.value != "none":
                    self.shadow.llm_stats["vetoes"] += 1
                else:
                    self.shadow.llm_stats["confirmations"] += 1
                self._emit("fusion", f"{cand.symbol} 真LLM融合：{fusion.final_signal} "
                                     f"(quant {fusion.quant_contribution.get('direction','?')}"
                                     f" × LLM {llm_sig.direction.value if llm_sig else 'unavailable'}"
                                     f" → {fusion.conflict_state.value})",
                           secid=cand.secid, symbol=cand.symbol, payload=fusion.to_dict())

            # ── Arm B: deterministic ensemble (preserved baseline) ──
            research_dir = ("bullish" if len(research.bull_case) > len(research.bear_case)
                            else "bearish" if len(research.bear_case) > len(research.bull_case)
                            else "neutral")
            arm_b_decision = self.ensemble.combine(EnsembleInput(
                symbol=cand.symbol, quant=qsig,
                llm_direction=research_dir, llm_confidence=research.confidence,
                llm_mode=research.research_mode,
            ))

            # ── Arm A: quant only ──
            quant_long = bool(qsig and qsig.direction == SignalDirection.LONG)
            quant_strength = float(getattr(qsig, "strength", 0.0) or 0.0)
            arm_a_buy = quant_long and quant_strength >= 0.6

            # ── DecisionDelta: record what each arm would do ──
            delta = DecisionDelta(cycle=cycle, symbol=cand.symbol,
                arm_decisions={
                    "quant_only": {"action": "buy" if arm_a_buy else "no_action",
                                   "strength": quant_strength},
                    "deterministic_hybrid": {"action": arm_b_decision.action_hint,
                                              "score": round(arm_b_decision.combined_score, 4)},
                    "llm_hybrid": {"action": fusion.final_signal if fusion else "no_action",
                                   "effective_mode": fusion.effective_mode if fusion else "quant_only_fallback",
                                   "strength": fusion.final_strength if fusion else 0.0},
                })
            if fusion is not None and arm_b_decision.action_hint != fusion.final_signal:
                delta.llm_changed_decision = True
                delta.change_summary = f"Arm B: {arm_b_decision.action_hint} → Arm C: {fusion.final_signal} (LLM)"
            self.shadow.record_delta(delta)

            # ── main account acts on the configured mode (default: Arm C when LLM active) ──
            # self.effective_mode is the TRUE provenance (computed from real
            # quant/llm state); fusion.effective_mode only exists on the LLM path.
            effective_mode = self.effective_mode
            if fusion is not None and fusion.final_signal == "buy":
                proposal = self.engine.build_buy_proposal(cand, research)
                if pred is not None and qsig is not None:
                    proposal.rationale.quant_view = {
                        "rank": pred.predicted_rank, "universe_size": pred.universe_size,
                        "predicted_return": pred.predicted_return,
                        "probability_up": pred.probability_up,
                        "horizon_days": pred.horizon_days,
                        "model": self.quant_result.model.get("display", ""),
                        "model_id": pred.model_version,
                        "calibrated": qsig.calibrated,
                        "feature_snapshot_id": pred.feature_snapshot_id,
                    }
                if llm_snap is not None:
                    proposal.rationale.llm_view = {
                        "thesis_direction": llm_snap.thesis_direction.value,
                        "bull_strength": llm_snap.bull_strength,
                        "bear_strength": llm_snap.bear_strength,
                        "thesis_confidence": llm_snap.thesis_confidence,
                        "citation_support": llm_snap.citation_support,
                        "contradiction_score": llm_snap.contradiction_score,
                        "unknown_risk_score": llm_snap.unknown_risk_score,
                        "investigation_id": llm_snap.investigation_id,
                        "model": llm_snap.model, "context_id": llm_snap.context_id,
                        "pit_safe": llm_snap.pit_safe,
                        "fusion": fusion.to_dict() if fusion else None,
                    }
                proposal.decision_quality = "hybrid_llm_quant"
                proposal.decision_mode = effective_mode
                # fusion multiplier into weight chain
                current_weights = {
                    sym: (pos.market_value / self.broker.account.equity)
                    for sym, pos in self.broker.positions.items()
                    if pos.quantity > 0 and self.broker.account.equity > 0}
                ptarget = self.portfolio_optimizer.construct(
                    {cand.symbol: {"expected_return": (pred.predicted_return if pred else None) or 0.0,
                                   "volatility": cand.signals.get("volatility_20d", 0.0)}},
                    current_weights)
                quant_alpha_target = proposal.target_weight
                optimizer_target = ptarget.weights.get(cand.symbol, quant_alpha_target)
                mult = fusion.final_strength if fusion and fusion.final_strength > 0 else 1.0
                pre_risk = min(quant_alpha_target, optimizer_target) * mult if optimizer_target > 0 else quant_alpha_target * mult
                proposal.rationale.target_weight_reason = {
                    **proposal.rationale.target_weight_reason,
                    "quant_alpha_target": round(quant_alpha_target, 4),
                    "portfolio_optimizer": {"method": ptarget.method,
                                            "target": round(optimizer_target, 4),
                                            "turnover_penalty": self.portfolio_optimizer.turnover_penalty},
                    "fusion_multiplier": round(mult, 4),
                    "pre_risk_target": round(pre_risk, 4),
                }
                proposal.target_weight = round(pre_risk, 4)
            elif fusion is not None and fusion.final_signal in ("reduce", "sell"):
                # LLM-driven position management: quant bear + LLM bear = reduce/sell
                pos = self.broker.positions.get(cand.symbol)
                qv = {"rank": pred.predicted_rank, "universe_size": pred.universe_size,
                      "predicted_return": pred.predicted_return} if pred else {}
                lv = {"thesis_direction": llm_snap.thesis_direction.value, "bull_strength": llm_snap.bull_strength,
                      "bear_strength": llm_snap.bear_strength, "conflict_score": llm_snap.contradiction_score,
                      "fusion": fusion.to_dict()} if llm_snap else {}
                if pos and pos.quantity > 0:
                    is_close = fusion.final_signal == "sell"
                    proposal = TradeProposal(
                        symbol=cand.symbol,
                        action=TradeAction.CLOSE if is_close else TradeAction.REDUCE,
                        target_weight=0.0 if is_close else 0.5,
                        rationale=TradeRationale(
                            thesis=(f"真LLM研究触发{fusion.final_signal}：{cand.symbol} "
                                    f"quant {fusion.quant_contribution.get('direction','?')} × "
                                    f"LLM {fusion.llm_contribution.get('direction','?')} → "
                                    f"conflict {fusion.conflict_state.value}"),
                            confidence=fusion.final_strength, quant_view=qv, llm_view=lv),
                        confidence=fusion.final_strength,
                        risk_result={"exit_reason": "llm_triggered"})
                    proposal.decision_quality = "hybrid_llm_quant"
                    proposal.decision_mode = effective_mode
                    self._execute_proposal(proposal, cand.quote, summary,
                                           exit_verdict="llm_triggered", research=research)
                    continue
            elif (arm_a_buy or not quant_long) and arm_b_decision.action_hint == "buy":
                # arm_a_buy OR quant unavailable: both buy signals OR quant absent
                # but deterministic ensemble says buy → proceed
                proposal = self.engine.build_buy_proposal(cand, research)
                if pred is not None and qsig is not None:
                    proposal.rationale.quant_view = {
                        "rank": pred.predicted_rank, "universe_size": pred.universe_size,
                        "predicted_return": pred.predicted_return,
                        "probability_up": pred.probability_up,
                        "horizon_days": pred.horizon_days,
                        "model": self.quant_result.model.get("display", ""),
                        "model_id": pred.model_version,
                        "calibrated": qsig.calibrated,
                    }
                proposal.decision_mode = effective_mode
                # weight chain (portfolio optimizer path shared with fusion=buy branch)
                current_weights = {
                    sym: (pos.market_value / self.broker.account.equity)
                    for sym, pos in self.broker.positions.items()
                    if pos.quantity > 0 and self.broker.account.equity > 0}
                ptarget = self.portfolio_optimizer.construct(
                    {cand.symbol: {"expected_return": (pred.predicted_return if pred else None) or 0.0,
                                   "volatility": cand.signals.get("volatility_20d", 0.0)}},
                    current_weights)
                quant_alpha_target = proposal.target_weight
                optimizer_target = ptarget.weights.get(cand.symbol, quant_alpha_target)
                pre_risk = min(quant_alpha_target, optimizer_target) if optimizer_target > 0 else quant_alpha_target
                proposal.rationale.target_weight_reason = {
                    **proposal.rationale.target_weight_reason,
                    "quant_alpha_target": round(quant_alpha_target, 4),
                    "portfolio_optimizer": {"method": ptarget.method,
                                            "target": round(optimizer_target, 4),
                                            "turnover_penalty": self.portfolio_optimizer.turnover_penalty},
                    "pre_risk_target": round(pre_risk, 4),
                }
                proposal.target_weight = round(pre_risk, 4)
            elif arm_b_decision.conflict is not None:
                self._emit("conflict", f"{cand.symbol} Quant/Research 信号冲突："
                                       f"{arm_b_decision.conflict.quant_view} vs "
                                       f"{arm_b_decision.conflict.llm_view} → "
                                       f"{arm_b_decision.conflict.resolution.value}",
                           secid=cand.secid, symbol=cand.symbol,
                           payload=arm_b_decision.conflict.to_dict())
                summary["no_actions"].append(f"{cand.symbol}:signal_conflict")
                continue
            else:
                self._emit("no_action", f"{cand.symbol} 融合信号 "
                                        f"{fusion.final_signal if fusion else arm_b_decision.action_hint}"
                                        f"，未达买入阈值",
                           secid=cand.secid, symbol=cand.symbol,
                           payload={"reason": "fusion_no_buy",
                                    "fusion": fusion.to_dict() if fusion else {}})
                summary["no_actions"].append(f"{cand.symbol}:fusion_no_buy")
                continue

            proposal.investigation_id = f"auto-{self.session.session_id}-c{cycle}"
            self._proposal_cooldown[cand.symbol] = cycle
            executed = self._execute_proposal(proposal, cand.quote, summary, research=research)
            if executed:
                buys += 1
                thesis = InvestmentThesis(
                    symbol=cand.symbol, status=ThesisStatus.ACTIVE,
                    thesis_text=research.thesis,
                    supporting_evidence_ids=research.evidence_ids(),
                    risks=research.risks,
                    invalidation_conditions=research.invalidation_conditions,
                    confidence=research.confidence, proposal_id=proposal.proposal_id,
                )
                self.theses[thesis.thesis_id] = thesis
                self._save_thesis(thesis)
                pos = self.broker.positions.get(cand.symbol)
                if pos:
                    pos.active_thesis_id = thesis.thesis_id
                self._emit("thesis", f"{cand.symbol} 建立投资论点（ACTIVE）", symbol=cand.symbol,
                           payload={"thesis_id": thesis.thesis_id, "proposal_id": proposal.proposal_id,
                                    "thesis": research.thesis})
        if not summary["actions"]:
            reason = ("本轮扫描无高于阈值的候选且持仓无需调整"
                      if candidates else "候选池全部无法取得行情数据")
            top = [{"symbol": c.symbol, "score": round(c.score, 3)} for c in candidates[:5]]
            self._emit("no_action", f"NO_ACTION：{reason}（阈值 {threshold}）",
                       payload={"reason": "no_qualifying_candidate", "threshold": threshold,
                                "top_scores": top})
            summary["no_actions"].append("no_qualifying_candidate")

        self._finish_cycle(summary)
        return summary

    # ── system truth ── the honest, un-fakeable component state

    def system_truth(self) -> dict[str, Any]:
        """The real state of every brain in the decision chain. Nothing here
        is aspirational: each field is read from the actual last run, so the
        UI can never claim a capability the system does not currently have."""
        quant = self.quant_result
        quant_ok = bool(quant and not quant.error and quant.predictions)
        # market data freshness: does the gateway hold any live/delayed quote?
        market_ok = False
        fresh_quotes = 0
        try:
            for q in market_gateway.get_all_latest():
                fr = (q.get("freshness") if isinstance(q, dict)
                      else getattr(q, "freshness", None))
                if fr in ("live", "delayed"):
                    fresh_quotes += 1
            market_ok = fresh_quotes > 0
        except Exception:
            pass

        llm_requested = self.session.research_mode == "llm"
        llm_active = self.trading_research is not None

        def comp(status: str, detail: str = "", **extra: Any) -> dict[str, Any]:
            return {"status": status, "detail": detail, **extra}

        quant_model = (quant.model if quant else {}) or {}
        quant_component = (
            comp("HEALTHY",
                 f"{quant_model.get('display', '')} · {len(quant.predictions)} 预测",
                 model=quant_model.get("display"),
                 model_id=quant_model.get("model_id"),
                 predictions=len(quant.predictions) if quant else 0,
                 fresh_instruments=len(quant.predictions) if quant else 0,
                 stale_instruments=len(quant.skipped) if quant else 0,
                 as_of=quant.as_of if quant else "")
            if quant_ok else
            comp("UNAVAILABLE",
                 quant.error if quant else "尚未运行推理",
                 model=quant_model.get("display"),
                 stale_instruments=len(quant.skipped) if quant else 0,
                 skipped=(quant.skipped[:8] if quant else []))
        )

        llm_component = (
            comp("HEALTHY", f"{self.session.research_mode} 研究已接入")
            if llm_active else
            comp("UNAVAILABLE",
                 self.llm_init_error or ("未启用 LLM 研究模式" if not llm_requested
                                         else "LLM 研究不可用"),
                 requested=llm_requested)
        )

        broker_ok = not self.broker.kill_switch_active
        return {
            "effective_mode": self.effective_mode,
            "components": {
                "market_data": comp("HEALTHY" if market_ok else "DEGRADED",
                                    f"{fresh_quotes} 只实时/延时报价" if market_ok
                                    else "无新鲜报价（可能休市或数据源不可达）",
                                    fresh_quotes=fresh_quotes),
                "quant": quant_component,
                "llm": llm_component,
                "research": comp("ACTIVE", "确定性信号研究始终可用"),
                "risk": comp("HEALTHY", f"已拒绝 {self.session.risk_rejections} 次"),
                "virtual_broker": comp("HEALTHY" if broker_ok else "HALTED",
                                       "撮合正常" if broker_ok else "Kill switch 已激活"),
            },
            "activity": {
                "stage": self.current_stage,
                "target": self.current_target,
                "cycle": self.session.cycle_count,
            },
        }

    # ── execution path ──

    def _persist_proposal(self, proposal: TradeProposal) -> None:
        """Upsert the proposal row with its CURRENT status. Called at every
        lifecycle transition so no reader ever sees an executed proposal as
        'pending'."""
        try:
            self.store.save_proposal(self.session.session_id, self.session.cycle_count, proposal)
        except Exception:
            logger.exception("proposal persistence failed")

    def _execute_proposal(self, proposal: TradeProposal, quote: dict[str, Any],
                          summary: dict[str, Any], research: ResearchOutcome | None = None,
                          exit_verdict: str = "", thesis: InvestmentThesis | None = None) -> bool:
        session = self.session
        symbol = proposal.symbol
        secid = self._secid_by_symbol.get(symbol, "")
        price, fx_info = self._account_price(quote)
        if price <= 0:
            self._emit("no_action", f"{symbol} 无法换算到账户货币 {session.currency}"
                                    f"（标的货币 {quote.get('currency')}，FX 数据不可用），跳过",
                       symbol=symbol, payload={"proposal_id": proposal.proposal_id,
                                               "reason": "fx_unavailable"})
            summary["no_actions"].append(f"{symbol}:fx_unavailable")
            return False

        if session.status == SessionStatus.HALTED or self.broker.kill_switch_active:
            proposal.status = "rejected"
            self._persist_proposal(proposal)
            self._emit("risk", f"{symbol} 提案被拒：会话已紧急停止", symbol=symbol,
                       payload={"proposal_id": proposal.proposal_id, "reason": "halted"})
            return False

        proposal.status = "pending"
        self._persist_proposal(proposal)
        self._emit("proposal", f"{symbol} 生成 {proposal.action.value.upper()} 提案，"
                               f"目标权重 {proposal.target_weight:.1%}，置信度 {proposal.confidence:.2f}",
                   secid=secid, symbol=symbol,
                   payload={"proposal_id": proposal.proposal_id, "proposal": proposal.to_dict(),
                            "quote_at_decision": quote,
                            "research_mode": research.research_mode if research else "rule",
                            "exit_verdict": exit_verdict})

        tradable, freshness = self._tradable(quote)
        result = self.risk.check(
            proposal, self.broker.account, self.broker.positions,
            list(self.broker.orders.values()), price,
            market_data_fresh=tradable,
        )
        risk_payload = result.to_payload()
        proposal.risk_result = proposal.risk_result | risk_payload
        self._emit("risk", f"{symbol} 风控：{'通过' if result.approved else '拒绝'}"
                           f"{'（已调整 ' + f'{result.original_target_weight:.1%}→{result.adjusted_target_weight:.1%}' + '）' if result.adjusted else ''}"
                           f" — {result.reason}",
                   secid=secid, symbol=symbol,
                   payload={"proposal_id": proposal.proposal_id, "risk_result": risk_payload})
        if not result.approved:
            proposal.status = "rejected"
            self._persist_proposal(proposal)
            session.risk_rejections += 1
            summary["no_actions"].append(f"{symbol}:risk_rejected")
            return False
        proposal.status = "adjusted" if result.adjusted else "approved"
        self._persist_proposal(proposal)

        # Sizing
        final_weight = result.adjusted_target_weight if result.adjusted else proposal.target_weight
        if proposal.action in (TradeAction.SELL, TradeAction.CLOSE, TradeAction.REDUCE):
            position = self.broker.positions.get(symbol)
            if not position or position.quantity <= 0:
                return False
            if proposal.action == TradeAction.REDUCE:
                qty = position.quantity * (proposal.target_weight if 0 < proposal.target_weight < 1 else 0.5)
            else:
                qty = position.quantity
            side = OrderSide.SELL
        else:
            qty, sized_weight = self.sizing.size(proposal, self.broker.account,
                                                 self.broker.positions.get(symbol), price)
            target_notional = self.broker.account.equity * final_weight
            qty = min(qty, target_notional / price if price > 0 else 0)
            side = OrderSide.BUY

        # Lot rules: CN trades in 100-share lots; US whole shares
        sec = parse_secid(secid) if secid else None
        if sec and sec.market == Market.CN:
            lot_size = 100
            qty = math.floor(qty / lot_size) * lot_size
        else:
            lot_size = 1
            qty = math.floor(qty)

        # ── sizing explainability event ──
        expected_amount = qty * price
        self._emit("sizing", f"{symbol} 仓位计算：{qty} 股 @ {price:.2f}，"
                             f"预计金额 {expected_amount:,.0f}（权重 {final_weight:.2%}，"
                             f"资产 {self.broker.account.equity:,.0f}，最小单位 {lot_size} 股）",
                   secid=secid, symbol=symbol,
                   payload={
                       "proposal_id": proposal.proposal_id,
                       "equity": self.broker.account.equity,
                       "final_weight": final_weight,
                       "price": price,
                       "raw_quantity": round(self.broker.account.equity * final_weight / price, 1) if price > 0 else 0,
                       "actual_quantity": qty,
                       "expected_amount": round(expected_amount, 2),
                       "lot_size": lot_size,
                       "commission_estimate": round(qty * 0.005, 2),
                   })
        if qty <= 0:
            proposal.status = "rejected"
            self._persist_proposal(proposal)
            self._emit("no_action", f"{symbol} 定量后数量为 0（价格 {price}，权重 {final_weight:.1%}），不下单",
                       symbol=symbol, payload={"proposal_id": proposal.proposal_id, "reason": "zero_quantity"})
            summary["no_actions"].append(f"{symbol}:zero_quantity")
            return False

        pre_position = self.broker.positions.get(symbol)
        pre_avg_cost = pre_position.average_cost if pre_position and pre_position.quantity > 0 else 0.0
        order = Order(
            account_id=self.broker.account.account_id, symbol=symbol, side=side,
            order_type=OrderType.MARKET, quantity=qty, proposal_id=proposal.proposal_id,
        )
        self._emit("order", f"{symbol} 提交 {side.value.upper()} 市价单 ×{qty}", secid=secid, symbol=symbol,
                   payload={"proposal_id": proposal.proposal_id, "order_id": order.order_id,
                            "quantity": qty, "price_ref": price, "freshness": freshness,
                            "instrument_price": quote.get("price", 0.0),
                            "instrument_currency": quote.get("currency", ""),
                            "account_currency": session.currency,
                            "fx": fx_info})
        submitted = self.broker.submit_order(order, price, proposal.proposal_id,
                                             instrument_meta={
                                                 "secid": secid,
                                                 "name": quote.get("name", symbol),
                                                 "market": quote.get("market", ""),
                                                 "currency": quote.get("currency", ""),
                                             })
        if submitted.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            proposal.status = "rejected"
            self._persist_proposal(proposal)
            self._emit("error", f"{symbol} 订单未成交：{submitted.error or submitted.status.value}",
                       symbol=symbol, payload={"proposal_id": proposal.proposal_id,
                                               "order": submitted.to_dict()})
            return False

        proposal.status = "executed"
        self._persist_proposal(proposal)
        fill = self.broker.fills[-1]
        self.broker.decisions.append(proposal.to_dict())
        self._emit("fill", f"{symbol} 成交 {side.value.upper()} ×{fill.quantity} @ {fill.price:.2f}"
                           f"（佣金 {fill.commission:.2f}）",
                   secid=secid, symbol=symbol,
                   payload={"proposal_id": proposal.proposal_id, "fill": fill.to_dict(),
                            "order_id": order.order_id})

        # ── decision story ── the full provenance chain in one event
        self._emit("decision", f"{symbol} {proposal.action.value.upper()} 决策完成："
                               f"{qty} 股 @ {fill.price:.2f}，"
                               f"权重 {final_weight:.2%}，"
                               f"置信度 {proposal.confidence:.2f}",
                   secid=secid, symbol=symbol,
                   payload={
                       "proposal_id": proposal.proposal_id,
                       "decision_id": f"d-{proposal.proposal_id}",
                       "action": proposal.action.value,
                       "symbol": symbol,
                       "final_weight": final_weight,
                       "quantity": qty,
                       "price": fill.price,
                       "amount": round(fill.quantity * fill.price, 2),
                       "decision_quality": proposal.decision_quality,
                       "research_mode": research.research_mode if research else "rule",
                       "status": "executed",
                   })
        position = self.broker.positions.get(symbol)
        self._emit("position", f"{symbol} 持仓更新：{position.quantity if position else 0} 股，"
                               f"现金 {self.broker.account.cash:,.0f}，总资产 {self.broker.account.equity:,.0f}",
                   secid=secid, symbol=symbol,
                   payload={"proposal_id": proposal.proposal_id,
                            "position": position.to_dict() if position else None,
                            "cash": self.broker.account.cash, "equity": self.broker.account.equity})
        # Close out thesis on exits
        if side == OrderSide.SELL and thesis is not None:
            exit_reason_str = proposal.risk_result.get("exit_reason", "manual")
            try:
                exit_reason = ExitReason(exit_reason_str)
            except ValueError:
                exit_reason = ExitReason.MANUAL
            # cost basis captured BEFORE the fill: after a full CLOSE the
            # position is already reset and its average_cost would read 0,
            # inflating realized PnL to the whole notional
            realized = (fill.price - pre_avg_cost) * fill.quantity
            thesis.close(exit_reason, proposal.rationale.thesis, realized)
            self._save_thesis(thesis)
            self._emit("exit", f"{symbol} 退出：{exit_reason.value} — {proposal.rationale.thesis}",
                       symbol=symbol,
                       payload={"proposal_id": proposal.proposal_id, "thesis_id": thesis.thesis_id,
                                "exit_reason": exit_reason.value, "realized_pnl": realized,
                                "original_thesis": thesis.thesis_text})
        summary["actions"].append({"symbol": symbol, "action": proposal.action.value,
                                   "quantity": fill.quantity, "price": fill.price,
                                   "proposal_id": proposal.proposal_id})
        return True

    def _finish_cycle(self, summary: dict[str, Any]) -> None:
        session = self.session
        # Refresh marks for all positions with latest quotes
        prices: dict[str, float] = {}
        for symbol in self.broker.positions:
            secid = self._secid_by_symbol.get(symbol)
            quote = self.feed.get_quote(secid) if secid else None
            if quote:
                acct_price, _ = self._account_price(quote)
                if acct_price > 0:
                    prices[symbol] = acct_price
        if prices:
            self.broker.update_market_prices(prices)
        session.current_equity = self.broker.account.equity
        session.cash = self.broker.account.cash
        market_value = sum(p.market_value for p in self.broker.positions.values())
        self.store.add_equity_point(session.session_id, session.cycle_count,
                                    session.current_equity, session.cash, market_value)
        self.store.save_broker_state(session.session_id, self.broker.to_state())
        # ── Phase Q5: shadow portfolios (cycle-end mark) ──
        day_returns: dict[str, float] = {}
        for sym in set(self.shadow.books["quant_only"].weights) | set(
                self.shadow.books["deterministic_hybrid"].weights) | set(
                self.shadow.books["llm_hybrid"].weights):
            pos = self.broker.positions.get(sym)
            if pos and pos.market_value > 0 and pos.average_cost > 0 and pos.quantity > 0:
                day_returns[sym] = (pos.market_price / pos.average_cost) - 1.0 if pos.average_cost else 0.0
        arm_targets: dict[str, dict[str, float]] = {}
        if self.quant_result is not None:
            # Arm A: quant top-5 equally
            top = sorted(
                [c for c in self.quant_result.signals if c and c.direction == SignalDirection.LONG],
                key=lambda s: s.strength, reverse=True)[:5]
            if top:
                arm_targets["quant_only"] = {s.symbol: 1.0 / len(top) for s in top}
        arm_targets["deterministic_hybrid"] = {
            pos.symbol: (pos.market_value / self.broker.account.equity
                         if self.broker.account.equity > 0 else 0.0)
            for pos in self.broker.positions.values() if pos.quantity > 0}
        arm_targets["llm_hybrid"] = arm_targets["deterministic_hybrid"]
        self.shadow.step(session.cycle_count, day_returns, arm_targets)
        self.store.save_session(session)
        # Persist a truth snapshot so the /system-truth endpoint stays honest
        # even for a loop restored after a restart (which hasn't re-run quant).
        try:
            self._emit("truth", "系统真相快照", payload=self.system_truth())
        except Exception:
            logger.exception("system truth snapshot failed")
        summary["equity"] = session.current_equity
        summary["cash"] = session.cash
        summary["shadow"] = self.shadow.summary()


class TradingScheduler:
    """Runs the loop continuously in a daemon thread with pause/halt control."""

    def __init__(self) -> None:
        self._threads: dict[str, threading.Thread] = {}
        self._loops: dict[str, AutonomousTradingLoop] = {}

    def get_loop(self, session_id: str) -> AutonomousTradingLoop | None:
        return self._loops.get(session_id)

    def register(self, loop: AutonomousTradingLoop) -> None:
        self._loops[loop.session.session_id] = loop

    def start(self, loop: AutonomousTradingLoop) -> None:
        session = loop.session
        self.register(loop)
        if session.session_id in self._threads and self._threads[session.session_id].is_alive():
            return
        session.status = SessionStatus.RUNNING
        session.started_at = session.started_at or datetime.now(timezone.utc).isoformat()
        loop.store.save_session(session)

        def _run() -> None:
            while session.status in (SessionStatus.RUNNING, SessionStatus.PAUSED):
                if session.status == SessionStatus.RUNNING:
                    try:
                        loop.run_cycle()
                    except Exception as exc:
                        logger.exception("Trading cycle failed")
                        session.error = str(exc)
                        loop._emit("error", f"交易循环异常：{exc}", payload={"error": str(exc)})
                        loop.store.save_session(session)
                    session.next_cycle_at = datetime.fromtimestamp(
                        time.time() + session.scan_interval, tz=timezone.utc
                    ).isoformat()
                    loop.store.save_session(session)
                # sleep in small steps so pause/stop take effect quickly
                for _ in range(int(max(session.scan_interval, 1) * 10)):
                    if session.status not in (SessionStatus.RUNNING, SessionStatus.PAUSED):
                        return
                    if session.status == SessionStatus.PAUSED:
                        time.sleep(0.5)
                        break
                    time.sleep(0.1)

        thread = threading.Thread(target=_run, daemon=True, name=f"trading-{session.session_id}")
        self._threads[session.session_id] = thread
        thread.start()


trading_scheduler = TradingScheduler()
