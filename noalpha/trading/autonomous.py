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

from noalpha.i18n import DEFAULT_LANG, t
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
    MIN_QUANT_ENTRY_STRENGTH, FusionPolicy, TradingResearchService, default_trading_research_service,
    snapshot_to_signal,
)
from noalpha.trading.candidate_policy import CandidateSelectionPolicy
from noalpha.trading.shadow import (
    ArmDecision, DecisionDelta, ShadowArena, ShadowPortfolioPolicy, classify_change,
)
from noalpha.trading.domain import Order, OrderSide, OrderStatus, OrderType
from noalpha.trading.proposal import (
    DecisionMode, ExitReason, TradeAction, TradeProposal, TradeRationale,
)
from noalpha.trading.risk import (
    PositionSizingEngine, PreTradeRiskEngine, TradingDayRiskState,
)
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
    score: float                      # heuristic momentum score (a FEATURE, not a gate)
    signals: dict[str, float]
    quote: dict[str, Any]
    reason: str = ""
    # ── selection provenance (set by CandidateSelectionPolicy) ──
    candidate_source: str = ""        # quant | heuristic_fallback
    quant_rank: int | None = None
    quant_strength: float | None = None
    selection_reason: str = ""


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

    def __init__(self, profile: dict[str, float], lang: str = DEFAULT_LANG) -> None:
        self.profile = profile
        self.lang = lang
        self.agents = TradingAgentsOrchestrator(profile, lang)

    def _t(self, zh: str, en: str) -> str:
        return t(self.lang, zh, en)

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

    def research(self, candidate: Candidate, feed: MarketFeed,
                 quant_view: dict[str, Any] | None = None) -> ResearchOutcome:
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
            eid = ev("mom5", f"5-day momentum {s['mom_5d']:+.2%}", s["mom_5d"])
            bull.append(self._t(f"5日动量 {s['mom_5d']:+.2%}（{eid}）", f"5-day momentum {s['mom_5d']:+.2%} ({eid})"))
        elif s.get("mom_5d", 0) < -0.02:
            eid = ev("mom5", f"5-day momentum {s['mom_5d']:+.2%}", s["mom_5d"])
            bear.append(self._t(f"5日动量 {s['mom_5d']:+.2%}（{eid}）", f"5-day momentum {s['mom_5d']:+.2%} ({eid})"))
        if s.get("mom_20d", 0) > 0.05:
            eid = ev("mom20", f"20-day momentum {s['mom_20d']:+.2%}", s["mom_20d"])
            bull.append(self._t(f"20日动量 {s['mom_20d']:+.2%}（{eid}）", f"20-day momentum {s['mom_20d']:+.2%} ({eid})"))
        elif s.get("mom_20d", 0) < -0.05:
            eid = ev("mom20", f"20-day momentum {s['mom_20d']:+.2%}", s["mom_20d"])
            bear.append(self._t(f"20日动量 {s['mom_20d']:+.2%}（{eid}）", f"20-day momentum {s['mom_20d']:+.2%} ({eid})"))
        if s.get("volume_ratio", 0) >= 2.0:
            up = s.get("day_change_pct", 0) > 0
            direction = self._t("放量上行" if up else "放量下行",
                                "high-volume advance" if up else "high-volume decline")
            eid = ev("vol", f"volume ratio {s['volume_ratio']:.1f}x", s["volume_ratio"])
            (bull if up else bear).append(self._t(
                f"{direction}，量比 {s['volume_ratio']:.1f}x（{eid}）",
                f"{direction}, volume ratio {s['volume_ratio']:.1f}x ({eid})"))
        if s.get("price_vs_ma20") is not None:
            above = s.get("price_vs_ma20", 0) >= 0
            side = self._t("上方" if above else "下方", "above" if above else "below")
            eid = ev("ma20", f"price vs MA20 {s.get('price_vs_ma20', 0):+.2%}", s.get("price_vs_ma20", 0))
            (bull if above else bear).append(self._t(
                f"价格位于 20 日均线{side} {abs(s.get('price_vs_ma20', 0)):.2%}（{eid}）",
                f"price {side} the 20-day MA by {abs(s.get('price_vs_ma20', 0)):.2%} ({eid})"))
        vol = s.get("volatility_20d", 0)
        if vol > 0.03:
            risks.append(self._t(f"20日波动率偏高 {vol:.2%}", f"elevated 20-day volatility {vol:.2%}"))
        risks.append(self._t("动量策略在震荡市中失效风险",
                             "momentum strategies break down in range-bound markets"))

        confidence = min(0.9, max(0.1, candidate.score * (0.85 if vol > 0.03 else 1.0)))
        thesis = self._t(
            f"{candidate.name}({candidate.symbol}) 综合动量得分 {candidate.score:.2f}："
            f"5日 {s.get('mom_5d', 0):+.2%}、20日 {s.get('mom_20d', 0):+.2%}、"
            f"量比 {s.get('volume_ratio', 0):.1f}x。基于真实日K与实时报价的确定性信号研究"
            f"（非LLM，research_mode=deterministic）。",
            f"{candidate.name} ({candidate.symbol}) composite momentum score {candidate.score:.2f}: "
            f"5-day {s.get('mom_5d', 0):+.2%}, 20-day {s.get('mom_20d', 0):+.2%}, "
            f"volume ratio {s.get('volume_ratio', 0):.1f}x. Deterministic signal research over real "
            f"daily bars and live quotes (not an LLM; research_mode=deterministic).",
        )
        invalidation = [
            self._t("收盘价跌破20日均线且5日动量转负",
                    "close breaks below the 20-day MA and 5-day momentum turns negative"),
            self._t(f"持仓亏损超过止损线 {self.profile['stop_loss']:.0%}",
                    f"position loss exceeds the {self.profile['stop_loss']:.0%} stop"),
            self._t("放量下跌（量比≥2且当日跌幅≥3%）",
                    "high-volume decline (volume ratio ≥2 and day change ≤ −3%)"),
        ]
        # The Quant Analyst role must read the ACTIVE model's real view. Passing
        # nothing here made it silently fall back to the heuristic candidate
        # score while still calling itself "quant_analyst" — a名实不符 the
        # SYSTEM TRUTH principle forbids. When quant genuinely has no view the
        # role reports status=degraded and says so.
        agent_report = self.agents.run(
            symbol=candidate.symbol,
            name=candidate.name,
            score=candidate.score,
            signals=candidate.signals,
            evidence_ids=[e["evidence_id"] for e in evidence],
            quant_view=quant_view,
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
        ranking_note = self._t(
            f"本轮 {len(research.evidence)} 条信号证据，{n_bull} 利多 {n_bear} 利空",
            f"{len(research.evidence)} signal evidence items this cycle: {n_bull} bullish, {n_bear} bearish")

        agent_report = research.agent_report
        trader_plan = agent_report.trader_plan if agent_report else None
        debate = agent_report.debate if agent_report else None
        agent_summary = (
            self._t(
                f"TradingAgents 角色链路完成：多空共识 {debate.consensus}、"
                f"冲突 {debate.conflict_level:.2f}；交易员建议 {trader_plan.action_hint}。",
                f"TradingAgents role chain completed: bull/bear consensus {debate.consensus}, "
                f"conflict {debate.conflict_level:.2f}; trader suggests {trader_plan.action_hint}.")
            if debate and trader_plan else
            self._t("TradingAgents 角色链路不可用，回退确定性信号研究。",
                    "TradingAgents role chain unavailable; fell back to deterministic signal research.")
        )
        decision_summary = self._t(
            f"AI 选择买入 {candidate.name}({candidate.symbol})。"
            f"短中期动量信号共振——5日 {candidate.signals.get('mom_5d', 0):+.2%}、"
            f"20日 {candidate.signals.get('mom_20d', 0):+.2%}，"
            f"综合候选评分 {candidate.score:.2f}；"
            f"{agent_summary}"
            f" 当前未伪造基本面/新闻/情绪数据，最终仓位仍受外部硬风控约束。",
            f"The AI chose to buy {candidate.name} ({candidate.symbol}). "
            f"Short- and medium-term momentum agree — 5-day {candidate.signals.get('mom_5d', 0):+.2%}, "
            f"20-day {candidate.signals.get('mom_20d', 0):+.2%}, "
            f"composite candidate score {candidate.score:.2f}. "
            f"{agent_summary}"
            f" No fundamental, news or sentiment data was fabricated, and the final size remains "
            f"subject to external hard risk limits.",
        )

        catalysts: list[dict[str, Any]] = []
        if candidate.signals.get("mom_5d", 0) > 0.02:
            catalysts.append({
                "title": self._t("短期动量增强", "Short-term momentum strengthening"),
                "description": self._t(
                    f"5日动量 {candidate.signals['mom_5d']:+.2%}，显著跑赢基准",
                    f"5-day momentum {candidate.signals['mom_5d']:+.2%}, clearly ahead of the benchmark"),
                "evidence_ids": research.evidence_ids()[:3],
                "expected_time_horizon": "5-15 trading days",
                "confidence": min(0.85, 0.55 + abs(candidate.signals["mom_5d"])),
            })
        if candidate.signals.get("mom_20d", 0) > 0.05:
            catalysts.append({
                "title": self._t("中期趋势延续", "Medium-term trend continuation"),
                "description": self._t(
                    f"20日动量 {candidate.signals['mom_20d']:+.2%}，趋势同步确认",
                    f"20-day momentum {candidate.signals['mom_20d']:+.2%}, trend confirmed in sync"),
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
            self._t(f"5日动量 {candidate.signals.get('mom_5d', 0):+.2%}",
                    f"5-day momentum {candidate.signals.get('mom_5d', 0):+.2%}")
            if candidate.signals.get("mom_5d", 0) > 0.02 else None,
            self._t(f"20日动量 {candidate.signals.get('mom_20d', 0):+.2%}",
                    f"20-day momentum {candidate.signals.get('mom_20d', 0):+.2%}")
            if candidate.signals.get("mom_20d", 0) > 0.05 else None,
            self._t(f"候选评分 {candidate.score:.2f} 超过阈值 {self.profile['buy_score_threshold']:.2f}",
                    f"candidate score {candidate.score:.2f} clears the {self.profile['buy_score_threshold']:.2f} threshold"),
        ]
        negative_factors = [
            self._t("当前无基本面催化证据（技术信号策略固有局限）",
                    "no fundamental catalyst evidence (an inherent limit of a technical-signal strategy)"),
            self._t(f"波动率 {candidate.signals.get('volatility_20d', 0):.2%}",
                    f"volatility {candidate.signals.get('volatility_20d', 0):.2%}")
            if candidate.signals.get("volatility_20d", 0) > 0.03 else None,
            self._t("缺乏跨来源交叉验证（仅有行情数据）",
                    "no cross-source corroboration (market data only)"),
            (self._t(f"存在 {n_bear} 条空头信号", f"{n_bear} bearish signal(s) present") if n_bear > 0 else None),
        ]

        return TradeProposal(
            symbol=candidate.symbol,
            action=TradeAction.BUY,
            target_weight=round(target, 4),
            rationale=TradeRationale(
                thesis=research.thesis,
                signal_reason=self._t(
                    f"综合动量得分 {candidate.score:.2f}："
                    f"5日 {candidate.signals.get('mom_5d', 0):+.2%}、"
                    f"20日 {candidate.signals.get('mom_20d', 0):+.2%}、"
                    f"量比 {candidate.signals.get('volume_ratio', 0):.1f}x",
                    f"composite momentum score {candidate.score:.2f}: "
                    f"5-day {candidate.signals.get('mom_5d', 0):+.2%}, "
                    f"20-day {candidate.signals.get('mom_20d', 0):+.2%}, "
                    f"volume ratio {candidate.signals.get('volume_ratio', 0):.1f}x",
                ),
                decision_summary=decision_summary,
                why_this_instrument=self._t(
                    f"{candidate.name}({candidate.symbol}) 在本轮扫描中排名靠前"
                    f"（评分 {candidate.score:.2f}，阈值 {self.profile['buy_score_threshold']:.2f}）。"
                    f"{ranking_note}。"
                    f"当前无持仓冲突，满足 TradingAgents 交易员准入和外部风控前置条件。",
                    f"{candidate.name} ({candidate.symbol}) ranked near the top of this scan "
                    f"(score {candidate.score:.2f} vs threshold {self.profile['buy_score_threshold']:.2f}). "
                    f"{ranking_note}. "
                    f"No conflicting position exists, and it clears the TradingAgents trader gate and "
                    f"the external risk pre-conditions.",
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
                self._t(f"止损触发：持仓收益 {ret:+.2%} ≤ 止损线 {self.profile['stop_loss']:.0%}",
                        f"Stop-loss hit: position return {ret:+.2%} ≤ stop at {self.profile['stop_loss']:.0%}"), 0.9,
            ), "stop_loss"
        if ret >= self.profile["take_profit"]:
            return exit_proposal(
                TradeAction.CLOSE, 0.0, ExitReason.TAKE_PROFIT,
                self._t(f"止盈触发：持仓收益 {ret:+.2%} ≥ 止盈线 {self.profile['take_profit']:.0%}",
                        f"Take-profit hit: position return {ret:+.2%} ≥ target at {self.profile['take_profit']:.0%}"), 0.85,
            ), "take_profit"
        # New negative evidence: heavy-volume selloff
        if signals.get("volume_ratio", 0) >= 2.0 and signals.get("day_change_pct", 0) <= -3.0:
            return exit_proposal(
                TradeAction.REDUCE, 0.5, ExitReason.NEW_NEGATIVE_EVIDENCE,
                self._t(
                    f"新负面证据：放量下跌（量比 {signals.get('volume_ratio', 0):.1f}x，"
                    f"当日 {signals.get('day_change_pct', 0):+.1f}%），减仓一半",
                    f"New negative evidence: high-volume decline (volume ratio "
                    f"{signals.get('volume_ratio', 0):.1f}x, day {signals.get('day_change_pct', 0):+.1f}%); "
                    f"halving the position"), 0.75,
            ), "negative_evidence"
        # Thesis invalidation: momentum thesis broken
        if signals.get("price_vs_ma20", 0) < -0.02 and signals.get("mom_5d", 0) < -0.03:
            return exit_proposal(
                TradeAction.CLOSE, 0.0, ExitReason.THESIS_INVALIDATED,
                self._t(
                    f"论点失效：价格跌破20日均线 {signals.get('price_vs_ma20', 0):+.2%} "
                    f"且5日动量 {signals.get('mom_5d', 0):+.2%} 转负，动量论点不再成立",
                    f"Thesis invalidated: price broke below the 20-day MA "
                    f"({signals.get('price_vs_ma20', 0):+.2%}) and 5-day momentum turned negative "
                    f"({signals.get('mom_5d', 0):+.2%}); the momentum thesis no longer holds"), 0.8,
            ), "thesis_invalidated"
        hold_reason = self._t(
            f"{symbol} 继续持有：收益率 {ret:+.2%}，"
            f"未触发止损 {self.profile['stop_loss']:.0%}/止盈 {self.profile['take_profit']:.0%}，"
            f"动量论点未失效且无新负面证据。",
            f"{symbol} still held: return {ret:+.2%}, neither the {self.profile['stop_loss']:.0%} stop "
            f"nor the {self.profile['take_profit']:.0%} target was hit, the momentum thesis stands and "
            f"no new negative evidence appeared.",
        ) if ret is not None else self._t(
            f"{symbol} 持仓维持，当前无卖出信号。", f"{symbol} position maintained; no sell signal.")
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
        self.engine = DecisionEngine(profile, session.lang)
        self.risk = PreTradeRiskEngine(
            max_position_weight=profile["max_position_weight"],
            max_daily_loss_pct=profile["max_daily_loss_pct"],
        )
        # Restore today's risk counters. Without this a restart would re-anchor
        # start-of-day equity and realized PnL, clearing the daily loss limit —
        # a hard control that a crash loop could bypass by simply crashing.
        self.risk.day_state = TradingDayRiskState.from_dict(session.risk_day_state)
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
        self.fusion_policy = FusionPolicy(session.lang)
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
        # Candidate discovery: quant-led when an ACTIVE model is available, with
        # the old momentum heuristic demoted to an explicit fallback.
        self.candidate_policy = CandidateSelectionPolicy(
            heuristic_threshold=profile["buy_score_threshold"])
        self.last_selection: dict[str, Any] = {}
        # Three independent counterfactual books, sized by ONE shared policy so
        # only the DECISION differs between arms.
        self.shadow = ShadowArena(
            session.session_id, session.initial_capital,
            policy=ShadowPortfolioPolicy(base_weight=profile["base_target_weight"],
                                         max_weight=profile["max_position_weight"]))
        # per-cycle scratch: symbol -> ArmDecision per arm, and the ONE price map
        self._arm_decisions: dict[str, dict[str, ArmDecision]] = {}
        self._cycle_prices: dict[str, float] = {}
        self._cycle_trading_date: str = ""
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

    def _t(self, zh: str, en: str) -> str:
        return t(self.session.lang, zh, en)

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
        # Reset the per-cycle counterfactual scratch. Every arm decision and
        # every shadow mark this cycle comes from the quotes captured below —
        # one scan, one price map, three arms.
        self._arm_decisions = {}
        self._cycle_prices = {}
        self._cycle_trading_date = ""

        universe = self._universe()
        self.current_stage = "scan"
        self.current_target = ""
        self._emit("scan", self._t(
            f"扫描 {len(universe)} 只标的（universe={session.universe}，"
            f"markets={','.join(session.allowed_markets)}）",
            f"Scanned {len(universe)} instruments (universe={session.universe}, "
            f"markets={','.join(session.allowed_markets)})"),
                   payload={"universe_size": len(universe)})

        if not universe:
            self._emit("no_action", self._t(
                "候选池为空：watchlist 中没有属于允许市场的启用标的",
                "Candidate pool empty: no enabled watchlist instrument belongs to an allowed market"),
                       payload={"reason": "empty_universe"})
            summary["no_actions"].append("empty_universe")
            self._finish_cycle(summary)
            return summary

        # Stage 1: cheap scan
        candidates, skipped = self.engine.scan_universe(universe, self.feed)
        for msg in skipped:
            self._emit("scan", self._t(f"跳过 {msg}", f"Skipped {msg}"), payload={"skipped": msg})
        for c in candidates:
            self._secid_by_symbol[c.symbol] = c.secid
            # ONE price map, captured from the SAME quotes the decisions see.
            acct_price, _ = self._account_price(c.quote)
            if acct_price > 0:
                self._cycle_prices[c.symbol] = acct_price
            # The trading date comes from the DATA, not the wall clock, so a
            # historical replay rolls the risk day exactly like live trading.
            stamp = str(c.quote.get("timestamp") or "")[:10]
            if stamp > self._cycle_trading_date:
                self._cycle_trading_date = stamp

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
            self._emit("quant_full", self._t("量化推理快照", "Quant inference snapshot"),
                       payload=quant.to_dict())
        except Exception:
            logger.exception("quant snapshot persist failed")
        signal_by_symbol = {s.symbol: s for s in quant.signals}
        quant_ok = not quant.error and bool(quant.predictions)
        self.effective_mode = self._compute_mode(quant_ok=quant_ok)
        summary["effective_mode"] = self.effective_mode
        self._emit("mode", self._t(f"本轮有效决策模式：{self.effective_mode}",
                                   f"Effective decision mode this cycle: {self.effective_mode}"),
                   payload={"effective_mode": self.effective_mode,
                            "quant_available": quant_ok,
                            "quant_error": quant.error,
                            "llm_requested": session.research_mode == "llm",
                            "llm_active": self.trading_research is not None,
                            "llm_init_error": self.llm_init_error})
        if quant.error:
            self._emit("quant", self._t(
                f"量化推理不可用：{quant.error}（回退启发式评分）",
                f"Quant inference unavailable: {quant.error} (fell back to heuristic scoring)"),
                       payload={"error": quant.error, "skipped": quant.skipped[:10]})
        else:
            top = [{"symbol": p.symbol, "rank": p.predicted_rank,
                    "predicted_return": round(p.predicted_return or 0.0, 5),
                    "probability_up": p.probability_up}
                   for p in quant.predictions[:5]]
            self._emit("quant", self._t(
                f"量化推理完成：{quant.model.get('display')} 对 "
                f"{len(quant.predictions)} 只标的横截面排名"
                f"（horizon {quant.model.get('horizon_days')}d）",
                f"Quant inference complete: {quant.model.get('display')} ranked "
                f"{len(quant.predictions)} instruments cross-sectionally "
                f"(horizon {quant.model.get('horizon_days')}d)"),
                       payload={"model": quant.model, "top_predictions": top,
                                "universe_size": len(quant.predictions),
                                "skipped": quant.skipped[:10],
                                "as_of": quant.as_of})
        # ── Stage 1.6: candidate selection ──
        # Who gets to decide which names reach Research. With an ACTIVE quant
        # model that is the model's cross-sectional ranking; the momentum
        # heuristic is a fallback only, and the choice is recorded as real
        # provenance rather than implied by the log wording.
        predictions_by_symbol = {p.symbol: p for p in quant.predictions}
        selection = self.candidate_policy.select(
            candidates, quant_available=quant_ok,
            signals_by_symbol=signal_by_symbol,
            predictions_by_symbol=predictions_by_symbol,
            quant_error=quant.error)
        candidates = selection.selected
        self.last_selection = selection.to_dict()
        summary["candidate_source"] = selection.source
        self._emit("selection", self._t(
            f"候选筛选：{selection.source}（入选 {len(selection.selected)}，"
            f"淘汰 {len(selection.rejected)}）— {selection.reason}",
            f"Candidate selection: {selection.source} "
            f"({len(selection.selected)} selected, {len(selection.rejected)} rejected) — "
            f"{selection.reason}"),
                   payload=self.last_selection)

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
                self._cycle_prices.setdefault(symbol, acct_price)
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
                            thesis=self._t(
                                f"量化排名恶化：{symbol} 当前横截面排名 "
                                f"{pred.predicted_rank}/{pred.universe_size}（后1/3），"
                                f"预期{pred.horizon_days}日超额收益转负 "
                                f"{pred.predicted_return:+.2%}，减仓一半",
                                f"Quant rank deteriorated: {symbol} now ranks "
                                f"{pred.predicted_rank}/{pred.universe_size} (bottom third), with the "
                                f"predicted {pred.horizon_days}-day excess return turning negative at "
                                f"{pred.predicted_return:+.2%}; halving the position"),
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
                self._emit("position", self._t(f"{symbol} 持仓检查：{verdict}",
                                               f"{symbol} position check: {verdict}"), symbol=symbol,
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
            # NOTE: no `cand.score < threshold` gate here any more. Candidate
            # admission is owned by CandidateSelectionPolicy above, which uses
            # the quant ranking whenever an ACTIVE model is available.
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
                    self._emit("no_action", self._t(
                        f"{cand.symbol} 已持仓 {existing_weight:.1%}，"
                        f"剩余额度不足一手（{lot} 股），跳过加仓",
                        f"{cand.symbol} already held at {existing_weight:.1%}; remaining headroom is "
                        f"under one lot ({lot} shares), so no add-on"),
                               secid=cand.secid, symbol=cand.symbol,
                               payload={"reason": "position_near_cap",
                                        "existing_weight": existing_weight,
                                        "headroom_notional": round(headroom_notional, 2),
                                        "lot_notional": round(lot * acct_price, 2)})
                    summary["no_actions"].append(f"{cand.symbol}:position_near_cap")
                    continue
            tradable, freshness = self._tradable(cand.quote)
            if not tradable:
                self._emit("no_action", self._t(
                    f"{cand.symbol} 已入选候选（{cand.selection_reason}）但市场不可交易"
                    f"（freshness={freshness}）",
                    f"{cand.symbol} was selected ({cand.selection_reason}) but the "
                    f"market is not tradable (freshness={freshness})"),
                           secid=cand.secid, symbol=cand.symbol,
                           payload={"reason": "market_not_tradable", "freshness": freshness,
                                    "score": cand.score,
                                    "candidate_source": cand.candidate_source})
                summary["no_actions"].append(f"{cand.symbol}:market_not_tradable")
                continue
            self._emit("candidate", self._t(
                f"{cand.symbol} 入选研究候选（来源 {cand.candidate_source}）：{cand.selection_reason}",
                f"{cand.symbol} selected for research (source {cand.candidate_source}): "
                f"{cand.selection_reason}"),
                       secid=cand.secid, symbol=cand.symbol,
                       payload={"score": cand.score, "signals": cand.signals,
                                "candidate_source": cand.candidate_source,
                                "quant_rank": cand.quant_rank,
                                "quant_strength": cand.quant_strength,
                                "selection_reason": cand.selection_reason,
                                "heuristic_reason": cand.reason})
            researched += 1
            self.current_target = f"{cand.name}({cand.symbol})"

            qsig = signal_by_symbol.get(cand.symbol)
            pred = predictions_by_symbol.get(cand.symbol)
            quant_view = ({"rank": pred.predicted_rank, "universe_size": pred.universe_size,
                           "predicted_return": pred.predicted_return,
                           "probability_up": pred.probability_up,
                           "horizon_days": pred.horizon_days,
                           "confidence": qsig.confidence if qsig else None,
                           "model": (self.quant_result.model.get("display", "")
                                     if self.quant_result else ""),
                           "model_id": pred.model_version}
                          if pred is not None else None)

            # ── Arm A: quant only ──
            # Recorded BEFORE any research runs. If it were recorded after the
            # deterministic-confidence gate below, Arm B's research would be
            # silently suppressing Arm A's decisions and the comparison would
            # no longer be a counterfactual.
            quant_long = bool(qsig and qsig.direction == SignalDirection.LONG)
            quant_strength = float(getattr(qsig, "strength", 0.0) or 0.0)
            arm_a_buy = quant_long and MIN_QUANT_ENTRY_STRENGTH <= quant_strength <= 1.0
            self._record_arm_decision(ArmDecision(
                arm="quant_only", symbol=cand.symbol,
                action="buy" if arm_a_buy else "no_action",
                size_multiplier=1.0, source="quant_signal",
                detail={"direction": qsig.direction.value if qsig else "missing",
                        "strength": quant_strength}))

            # ── deterministic research (Arm B baseline, always computed) ──
            research = self.engine.research(cand, self.feed, quant_view=quant_view)
            self._emit("research", self._t(
                f"{cand.symbol} 研究完成：confidence={research.confidence:.2f}（deterministic 信号研究）",
                f"{cand.symbol} research complete: confidence={research.confidence:.2f} "
                f"(deterministic signal research)"),
                       secid=cand.secid, symbol=cand.symbol,
                       payload={"thesis": research.thesis, "bull_case": research.bull_case,
                                "bear_case": research.bear_case, "risks": research.risks,
                                "evidence": research.evidence, "confidence": research.confidence,
                                "research_mode": research.research_mode})
            if research.agent_report is not None:
                agent_payload = research.agent_report.to_dict()
                self._emit("agent", self._t(
                    f"{cand.symbol} TradingAgents 角色链路完成："
                    f"{agent_payload.get('debate', {}).get('consensus', 'neutral')} → "
                    f"{agent_payload.get('trader_plan', {}).get('action_hint', 'no_action')}",
                    f"{cand.symbol} TradingAgents role chain complete: "
                    f"{agent_payload.get('debate', {}).get('consensus', 'neutral')} → "
                    f"{agent_payload.get('trader_plan', {}).get('action_hint', 'no_action')}"),
                           secid=cand.secid, symbol=cand.symbol,
                           payload=agent_payload)
            if research.confidence < 0.5:
                # Research-dependent arms stand down; Arm A already decided.
                for arm in ("deterministic_hybrid", "llm_hybrid"):
                    self._record_arm_decision(ArmDecision(
                        arm=arm, symbol=cand.symbol, action="no_action",
                        source="research_confidence_gate",
                        detail={"confidence": research.confidence, "floor": 0.5}))
                self._emit("no_action", self._t(
                    f"{cand.symbol} 研究置信度 {research.confidence:.2f} < 0.5，放弃",
                    f"{cand.symbol} research confidence {research.confidence:.2f} < 0.5; abandoned"),
                           secid=cand.secid, symbol=cand.symbol,
                           payload={"reason": "low_confidence", "confidence": research.confidence})
                summary["no_actions"].append(f"{cand.symbol}:low_confidence")
                continue

            # ── real LLM research (Arm C, Phase Q5) ──
            llm_snap, llm_sig, fusion = None, None, None
            if self.trading_research is not None:
                day_change = cand.signals.get("day_change_pct", 0.0)
                llm_snap, snap_status = self.trading_research.research(
                    secid=cand.secid, symbol=cand.symbol,
                    market=cand.quote.get("market", ""),
                    day_change_pct=day_change)
                # These counters measure new research attempts. Cache hits and
                # denied attempts made no LLM call and incurred no new cost.
                if snap_status == "generated" or snap_status.startswith("failed:"):
                    self.shadow.llm_stats["calls"] += 1
                if llm_snap is not None:
                    llm_sig = snapshot_to_signal(llm_snap)
                    if snap_status == "generated":
                        self.shadow.llm_stats["tokens"] += llm_snap.input_tokens + llm_snap.output_tokens
                        self.shadow.llm_stats["latency_ms_total"] += llm_snap.latency_ms
                elif snap_status.startswith("failed:"):
                    self.shadow.llm_stats["failures"] += 1
                # always call fusion (handles llm=None internally as fallback)
                fusion = self.fusion_policy.fuse(
                    symbol=cand.symbol, secid=cand.secid, quant=qsig, llm=llm_sig)
                if fusion.effective_mode != "hybrid_llm_quant":
                    self.shadow.llm_stats["fallback_cycles"] += 1
                fusion_detail = (f"(quant {fusion.quant_contribution.get('direction','?')}"
                                 f" × LLM {llm_sig.direction.value if llm_sig else 'unavailable'}"
                                 f" → {fusion.conflict_state.value})")
                self._emit("fusion", self._t(
                    f"{cand.symbol} 研究融合：{fusion.final_signal} {fusion_detail}",
                    f"{cand.symbol} research fusion: {fusion.final_signal} {fusion_detail}"),
                           secid=cand.secid, symbol=cand.symbol,
                           payload={**fusion.to_dict(), "research_status": snap_status})

            # ── Arm B: deterministic ensemble (preserved baseline) ──
            research_dir = ("bullish" if len(research.bull_case) > len(research.bear_case)
                            else "bearish" if len(research.bear_case) > len(research.bull_case)
                            else "neutral")
            arm_b_decision = self.ensemble.combine(EnsembleInput(
                symbol=cand.symbol, quant=qsig,
                llm_direction=research_dir, llm_confidence=research.confidence,
                llm_mode=research.research_mode,
            ))

            # ── Arm B / Arm C decisions ──
            # Arm C falls back to Arm B's decision when the LLM is not wired in,
            # and the delta below then correctly records "no change" instead of
            # pretending the LLM abstained.
            self._record_arm_decision(ArmDecision(
                arm="deterministic_hybrid", symbol=cand.symbol,
                action=arm_b_decision.action_hint, size_multiplier=1.0,
                source="deterministic_ensemble",
                detail={"combined_score": round(arm_b_decision.combined_score, 4),
                        "research_direction": research_dir,
                        "research_confidence": research.confidence}))
            arm_c_action = fusion.final_signal if fusion is not None else arm_b_decision.action_hint
            llm_admitted = fusion is not None and fusion.effective_mode == "hybrid_llm_quant"
            arm_c_mult = (fusion.final_strength if fusion is not None
                          and fusion.final_strength > 0 else 1.0)
            self._record_arm_decision(ArmDecision(
                arm="llm_hybrid", symbol=cand.symbol,
                action=arm_c_action, size_multiplier=arm_c_mult,
                source=("llm_fusion" if llm_admitted else fusion.effective_mode
                        if fusion is not None else "deterministic_ensemble(no LLM)"),
                detail={"effective_mode": fusion.effective_mode if fusion else "llm_unavailable",
                        "conflict_state": fusion.conflict_state.value if fusion else "",
                        "final_strength": fusion.final_strength if fusion else 0.0}))

            # ── DecisionDelta: DECISION attribution only ──
            # This records that the LLM changed a decision. It deliberately
            # makes no claim about money: that is answered by the three
            # independent shadow books in portfolio attribution.
            delta = DecisionDelta(cycle=cycle, symbol=cand.symbol,
                arm_decisions={arm: d.to_dict()
                               for arm, d in self._arm_decisions.get(cand.symbol, {}).items()})
            if llm_admitted and arm_b_decision.action_hint != arm_c_action:
                delta.llm_changed_decision = True
                delta.change_type = classify_change(arm_b_decision.action_hint, arm_c_action)
                delta.change_summary = (f"Arm B: {arm_b_decision.action_hint} → "
                                        f"Arm C: {arm_c_action} (LLM)")
            if llm_admitted:
                if arm_b_decision.action_hint == "buy" and arm_c_action in ("no_action", "watch"):
                    self.shadow.llm_stats["vetoes"] += 1
                elif arm_b_decision.action_hint == arm_c_action:
                    self.shadow.llm_stats["confirmations"] += 1
            self.shadow.record_delta(delta)

            # ── main account acts on the configured mode (default: Arm C when LLM active) ──
            # Service availability is not proof that this candidate received
            # usable research. Persist its actual fallback on the proposal.
            effective_mode = (DecisionMode.QUANT_ONLY_FALLBACK.value
                              if fusion is not None and not llm_admitted else self.effective_mode)
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
                proposal.decision_quality = "hybrid_llm_quant" if llm_admitted else effective_mode
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
                            thesis=self._t(
                                f"真LLM研究触发{fusion.final_signal}：{cand.symbol} "
                                f"quant {fusion.quant_contribution.get('direction','?')} × "
                                f"LLM {fusion.llm_contribution.get('direction','?')} → "
                                f"conflict {fusion.conflict_state.value}",
                                f"Real-LLM research triggered {fusion.final_signal}: {cand.symbol} "
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
            elif fusion is None and (arm_a_buy or not quant_long) and arm_b_decision.action_hint == "buy":
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
                conflict_detail = (f"{arm_b_decision.conflict.quant_view} vs "
                                   f"{arm_b_decision.conflict.llm_view} → "
                                   f"{arm_b_decision.conflict.resolution.value}")
                self._emit("conflict", self._t(
                    f"{cand.symbol} Quant/Research 信号冲突：{conflict_detail}",
                    f"{cand.symbol} quant/research signal conflict: {conflict_detail}"),
                           secid=cand.secid, symbol=cand.symbol,
                           payload=arm_b_decision.conflict.to_dict())
                summary["no_actions"].append(f"{cand.symbol}:signal_conflict")
                continue
            else:
                signal_name = fusion.final_signal if fusion else arm_b_decision.action_hint
                self._emit("no_action", self._t(
                    f"{cand.symbol} 融合信号 {signal_name}，未达买入阈值",
                    f"{cand.symbol} fused signal {signal_name} did not clear the buy threshold"),
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
                self._emit("thesis", self._t(f"{cand.symbol} 建立投资论点（ACTIVE）",
                                             f"{cand.symbol} investment thesis opened (ACTIVE)"),
                           symbol=cand.symbol,
                           payload={"thesis_id": thesis.thesis_id, "proposal_id": proposal.proposal_id,
                                    "thesis": research.thesis})
        if not summary["actions"]:
            reason = (self._t("本轮扫描无高于阈值的候选且持仓无需调整",
                              "no candidate cleared the threshold this scan and no position needed adjusting")
                      if candidates else
                      self._t("候选池全部无法取得行情数据",
                              "no candidate in the pool had usable market data"))
            top = [{"symbol": c.symbol, "score": round(c.score, 3)} for c in candidates[:5]]
            self._emit("no_action", self._t(f"NO_ACTION：{reason}（阈值 {threshold}）",
                                            f"NO_ACTION: {reason} (threshold {threshold})"),
                       payload={"reason": "no_qualifying_candidate", "threshold": threshold,
                                "top_scores": top})
            summary["no_actions"].append("no_qualifying_candidate")

        self._finish_cycle(summary)
        return summary

    # ── shadow arms ──

    def _record_arm_decision(self, decision: ArmDecision) -> None:
        self._arm_decisions.setdefault(decision.symbol, {})[decision.arm] = decision

    def _shadow_prices(self) -> dict[str, float]:
        """The ONE price map for this cycle's shadow step.

        Starts from the quotes the decisions were actually made on, then fills
        in any instrument an arm holds but that was not scanned this cycle —
        otherwise an arm's holding would silently freeze at a stale mark and
        that arm's return would be understated.
        """
        prices = dict(self._cycle_prices)
        held: set[str] = set()
        for book in self.shadow.books.values():
            held |= {s for s, q in book.shares.items() if q > 0}
        for symbol in held - set(prices):
            secid = self._secid_by_symbol.get(symbol)
            if not secid:
                continue
            quote = self.feed.get_quote(secid)
            if not quote:
                continue
            price, _ = self._account_price(quote)
            if price > 0:
                prices[symbol] = price
        return prices

    def _shadow_step(self, cycle: int, signal_by_symbol: dict[str, Any]) -> dict[str, Any]:
        """Advance all three counterfactual books one cycle.

        Every arm gets its own decision list and its own target weights derived
        from its OWN book. Nothing here reads the real broker's positions, and
        nothing copies one arm's targets into another.
        """
        prices = self._shadow_prices()
        if not prices and not self._arm_decisions:
            # Nothing was priced and nothing was decided (e.g. an empty
            # universe). Recording a zero-return cycle would only inflate the
            # sample count toward "sufficient" without adding information.
            return {"skipped": True, "reason": "no priced instrument this cycle"}
        arm_targets: dict[str, dict[str, float]] = {}
        for arm, book in self.shadow.books.items():
            decisions = [per_arm[arm] for per_arm in self._arm_decisions.values()
                         if arm in per_arm]
            decided = {d.symbol for d in decisions}
            # Shared carry/exit rule for names this arm holds but that were not
            # researched this cycle. Identical for all three arms, so it cannot
            # bias the comparison — it is machinery, not a decision policy.
            for symbol in book.weights():
                if symbol in decided:
                    continue
                sig = signal_by_symbol.get(symbol)
                short = bool(sig and sig.direction == SignalDirection.SHORT)
                decisions.append(ArmDecision(
                    arm=arm, symbol=symbol,
                    action="reduce" if short else "hold",
                    source="shared_carry_rule",
                    detail={"quant_direction": (sig.direction.value if sig else "unknown")}))
            arm_targets[arm] = self.shadow.targets_for(arm, decisions)
        return self.shadow.step(cycle, prices, arm_targets)

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
                 self._t(f"{quant_model.get('display', '')} · {len(quant.predictions)} 预测",
                         f"{quant_model.get('display', '')} · {len(quant.predictions)} predictions"),
                 model=quant_model.get("display"),
                 model_id=quant_model.get("model_id"),
                 predictions=len(quant.predictions) if quant else 0,
                 fresh_instruments=len(quant.predictions) if quant else 0,
                 stale_instruments=len(quant.skipped) if quant else 0,
                 as_of=quant.as_of if quant else "")
            if quant_ok else
            comp("UNAVAILABLE",
                 quant.error if quant else self._t("尚未运行推理", "no inference has run yet"),
                 model=quant_model.get("display"),
                 stale_instruments=len(quant.skipped) if quant else 0,
                 skipped=(quant.skipped[:8] if quant else []))
        )

        llm_component = (
            comp("HEALTHY", self._t(f"{self.session.research_mode} 研究已接入",
                                    f"{self.session.research_mode} research is wired in"))
            if llm_active else
            comp("UNAVAILABLE",
                 self.llm_init_error or (
                     self._t("未启用 LLM 研究模式", "LLM research mode is not enabled") if not llm_requested
                     else self._t("LLM 研究不可用", "LLM research unavailable")),
                 requested=llm_requested)
        )

        broker_ok = not self.broker.kill_switch_active
        selection = self.last_selection or {}
        candidate_source = selection.get("source", "")
        shadow_sample = self.shadow.sample_state()
        day_state = self.risk.day_state.to_dict()
        return {
            "effective_mode": self.effective_mode,
            # ── who actually chose the candidates this cycle ──
            "candidate_selection": {
                "source": candidate_source or "not_run",
                "status": ("QUANT_LED" if candidate_source == "quant"
                           else "HEURISTIC_FALLBACK" if candidate_source
                           else "NOT_RUN"),
                "reason": selection.get("reason", ""),
                "selected": len(selection.get("selected", [])),
                "rejected": selection.get("rejected_total", 0),
                "policy": selection.get("policy", {}),
            },
            # ── can the shadow comparison support a claim yet? ──
            "shadow": {
                "status": ("SAMPLE_OK" if shadow_sample["sufficient"]
                           else "INSUFFICIENT_SAMPLE"),
                **shadow_sample,
            },
            # ── the trading-day risk counters the limits are gated on ──
            "risk_state": day_state,
            "components": {
                "market_data": comp("HEALTHY" if market_ok else "DEGRADED",
                                    self._t(f"{fresh_quotes} 只实时/延时报价",
                                            f"{fresh_quotes} live/delayed quotes") if market_ok
                                    else self._t("无新鲜报价（可能休市或数据源不可达）",
                                                 "no fresh quotes (market may be closed or the source unreachable)"),
                                    fresh_quotes=fresh_quotes),
                "quant": quant_component,
                "llm": llm_component,
                "research": comp("ACTIVE", self._t("确定性信号研究始终可用",
                                                   "deterministic signal research is always available")),
                "risk": comp("HEALTHY", self._t(f"已拒绝 {self.session.risk_rejections} 次",
                                                f"{self.session.risk_rejections} rejections so far")),
                "virtual_broker": comp("HEALTHY" if broker_ok else "HALTED",
                                       self._t("撮合正常", "fills operating normally") if broker_ok
                                       else self._t("Kill switch 已激活", "kill switch is active")),
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
            self._emit("no_action", self._t(
                f"{symbol} 无法换算到账户货币 {session.currency}"
                f"（标的货币 {quote.get('currency')}，FX 数据不可用），跳过",
                f"{symbol} cannot be converted to the account currency {session.currency} "
                f"(instrument currency {quote.get('currency')}, FX data unavailable); skipped"),
                       symbol=symbol, payload={"proposal_id": proposal.proposal_id,
                                               "reason": "fx_unavailable"})
            summary["no_actions"].append(f"{symbol}:fx_unavailable")
            return False

        if session.status == SessionStatus.HALTED or self.broker.kill_switch_active:
            proposal.status = "rejected"
            self._persist_proposal(proposal)
            self._emit("risk", self._t(f"{symbol} 提案被拒：会话已紧急停止",
                                       f"{symbol} proposal rejected: session is emergency-stopped"),
                       symbol=symbol,
                       payload={"proposal_id": proposal.proposal_id, "reason": "halted"})
            return False

        proposal.status = "pending"
        self._persist_proposal(proposal)
        self._emit("proposal", self._t(
            f"{symbol} 生成 {proposal.action.value.upper()} 提案，"
            f"目标权重 {proposal.target_weight:.1%}，置信度 {proposal.confidence:.2f}",
            f"{symbol} {proposal.action.value.upper()} proposal created, "
            f"target weight {proposal.target_weight:.1%}, confidence {proposal.confidence:.2f}"),
                   secid=secid, symbol=symbol,
                   payload={"proposal_id": proposal.proposal_id, "proposal": proposal.to_dict(),
                            "quote_at_decision": quote,
                            "research_mode": research.research_mode if research else "rule",
                            "exit_verdict": exit_verdict})

        tradable, freshness = self._tradable(quote)
        # The reference price for the bad-tick check is the previous close from
        # the SAME quote, converted into the account currency the same way.
        reference_price = 0.0
        prev_close = quote.get("previous_close") or 0.0
        if prev_close > 0:
            reference_price, _ = self._account_price({**quote, "price": prev_close})
        trading_date = str(quote.get("timestamp") or "")[:10] or self._cycle_trading_date
        result = self.risk.check(
            proposal, self.broker.account, self.broker.positions,
            list(self.broker.orders.values()), price,
            market_data_fresh=tradable,
            trading_date=trading_date,
            reference_price=reference_price,
            kill_switch=self.broker.kill_switch_active,
        )
        risk_payload = result.to_payload()
        proposal.risk_result = proposal.risk_result | risk_payload
        adj = f"{result.original_target_weight:.1%}→{result.adjusted_target_weight:.1%}"
        self._emit("risk", self._t(
            f"{symbol} 风控：{'通过' if result.approved else '拒绝'}"
            f"{f'（已调整 {adj}）' if result.adjusted else ''} — {result.reason}",
            f"{symbol} risk check: {'approved' if result.approved else 'rejected'}"
            f"{f' (adjusted {adj})' if result.adjusted else ''} — {result.reason}"),
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
        self._emit("sizing", self._t(
            f"{symbol} 仓位计算：{qty} 股 @ {price:.2f}，"
            f"预计金额 {expected_amount:,.0f}（权重 {final_weight:.2%}，"
            f"资产 {self.broker.account.equity:,.0f}，最小单位 {lot_size} 股）",
            f"{symbol} sizing: {qty} shares @ {price:.2f}, "
            f"expected amount {expected_amount:,.0f} (weight {final_weight:.2%}, "
            f"equity {self.broker.account.equity:,.0f}, lot size {lot_size})"),
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
            self._emit("no_action", self._t(
                f"{symbol} 定量后数量为 0（价格 {price}，权重 {final_weight:.1%}），不下单",
                f"{symbol} sized to zero shares (price {price}, weight {final_weight:.1%}); no order sent"),
                       symbol=symbol, payload={"proposal_id": proposal.proposal_id, "reason": "zero_quantity"})
            summary["no_actions"].append(f"{symbol}:zero_quantity")
            return False

        pre_position = self.broker.positions.get(symbol)
        pre_avg_cost = pre_position.average_cost if pre_position and pre_position.quantity > 0 else 0.0
        order = Order(
            account_id=self.broker.account.account_id, symbol=symbol, side=side,
            order_type=OrderType.MARKET, quantity=qty, proposal_id=proposal.proposal_id,
        )
        self._emit("order", self._t(f"{symbol} 提交 {side.value.upper()} 市价单 ×{qty}",
                                    f"{symbol} submitted {side.value.upper()} market order ×{qty}"),
                   secid=secid, symbol=symbol,
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
            self._emit("error", self._t(
                f"{symbol} 订单未成交：{submitted.error or submitted.status.value}",
                f"{symbol} order not filled: {submitted.error or submitted.status.value}"),
                       symbol=symbol, payload={"proposal_id": proposal.proposal_id,
                                               "order": submitted.to_dict()})
            return False

        proposal.status = "executed"
        self._persist_proposal(proposal)
        fill = self.broker.fills[-1]
        self.broker.decisions.append(proposal.to_dict())
        self._emit("fill", self._t(
            f"{symbol} 成交 {side.value.upper()} ×{fill.quantity} @ {fill.price:.2f}"
            f"（佣金 {fill.commission:.2f}）",
            f"{symbol} filled {side.value.upper()} ×{fill.quantity} @ {fill.price:.2f} "
            f"(commission {fill.commission:.2f})"),
                   secid=secid, symbol=symbol,
                   payload={"proposal_id": proposal.proposal_id, "fill": fill.to_dict(),
                            "order_id": order.order_id})

        # ── decision story ── the full provenance chain in one event
        self._emit("decision", self._t(
            f"{symbol} {proposal.action.value.upper()} 决策完成："
            f"{qty} 股 @ {fill.price:.2f}，权重 {final_weight:.2%}，置信度 {proposal.confidence:.2f}",
            f"{symbol} {proposal.action.value.upper()} decision complete: "
            f"{qty} shares @ {fill.price:.2f}, weight {final_weight:.2%}, "
            f"confidence {proposal.confidence:.2f}"),
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
        self._emit("position", self._t(
            f"{symbol} 持仓更新：{position.quantity if position else 0} 股，"
            f"现金 {self.broker.account.cash:,.0f}，总资产 {self.broker.account.equity:,.0f}",
            f"{symbol} position updated: {position.quantity if position else 0} shares, "
            f"cash {self.broker.account.cash:,.0f}, equity {self.broker.account.equity:,.0f}"),
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
            self._emit("exit", self._t(
                f"{symbol} 退出：{exit_reason.value} — {proposal.rationale.thesis}",
                f"{symbol} exit: {exit_reason.value} — {proposal.rationale.thesis}"),
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
        # Roll/refresh the trading-day risk counters every cycle, not only when
        # an order is proposed. Anchoring start-of-day equity on the first
        # PROPOSAL of the day would measure the drawdown from whenever the
        # system happened to want to trade, not from the open.
        self.risk.day_state.observe(
            trading_date=self._cycle_trading_date,
            equity=self.broker.account.equity,
            realized_pnl_total=sum(p.realized_pnl for p in self.broker.positions.values()),
            unrealized_pnl_total=sum(p.unrealized_pnl for p in self.broker.positions.values()))
        session.risk_day_state = self.risk.day_state.to_dict()
        market_value = sum(p.market_value for p in self.broker.positions.values())
        self.store.add_equity_point(session.session_id, session.cycle_count,
                                    session.current_equity, session.cash, market_value)
        self.store.save_broker_state(session.session_id, self.broker.to_state())
        # ── shadow arms: three independent counterfactual books ──
        # Each arm marks LAST cycle's holdings at THIS cycle's prices, then
        # trades toward its OWN targets. No arm reads the broker or another arm.
        signal_by_symbol = {s.symbol: s for s in (self.quant_result.signals
                                                  if self.quant_result else [])}
        try:
            self._shadow_step(session.cycle_count, signal_by_symbol)
        except Exception:
            logger.exception("shadow step failed (non-fatal to the real book)")
        self.store.save_session(session)
        # Persist a truth snapshot so the /system-truth endpoint stays honest
        # even for a loop restored after a restart (which hasn't re-run quant).
        try:
            self._emit("truth", self._t("系统真相快照", "System truth snapshot"),
                       payload=self.system_truth())
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
                        loop._emit("error", loop._t(f"交易循环异常：{exc}",
                                                    f"Trading loop error: {exc}"),
                                   payload={"error": str(exc)})
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
