"""TradingAgents-style role orchestration for the simulation engine.

The original autonomous loop remains the execution spine. This module adds a
structured "trading firm" decision layer inspired by TradingAgents:

  analysts -> bull/bear researchers -> trader -> risk guardians -> fund manager

It deliberately avoids pretending that unavailable data exists. Fundamental,
news and sentiment roles return explicit UNAVAILABLE reports until real data
feeds are wired in; technical and quant roles use the signals already produced
inside the loop.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..i18n import DEFAULT_LANG, t


@dataclass
class AgentReport:
    role: str
    stance: str = "neutral"
    confidence: float = 0.0
    summary: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    status: str = "available"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchDebate:
    bull: AgentReport
    bear: AgentReport
    consensus: str
    conflict_level: float
    balance_note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bull": self.bull.to_dict(),
            "bear": self.bear.to_dict(),
            "consensus": self.consensus,
            "conflict_level": self.conflict_level,
            "balance_note": self.balance_note,
        }


@dataclass
class TraderPlan:
    action_hint: str
    target_weight_hint: float
    confidence: float
    thesis: str
    why_now: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RiskGuardianReview:
    name: str
    verdict: str
    reason: str
    max_weight: float | None = None
    adjustment: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradingAgentsReport:
    framework: str = "TradingAgents-inspired:v1"
    analysts: list[AgentReport] = field(default_factory=list)
    debate: ResearchDebate | None = None
    trader_plan: TraderPlan | None = None
    risk_reviews: list[RiskGuardianReview] = field(default_factory=list)
    fund_manager: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "analysts": [a.to_dict() for a in self.analysts],
            "debate": self.debate.to_dict() if self.debate else None,
            "trader_plan": self.trader_plan.to_dict() if self.trader_plan else None,
            "risk_reviews": [r.to_dict() for r in self.risk_reviews],
            "fund_manager": self.fund_manager,
        }


class TradingAgentsOrchestrator:
    """Small deterministic role graph used by the trading loop.

    The orchestrator accepts the same candidate and signal objects the existing
    engine already uses. It returns a structured report; downstream risk and
    execution remain deterministic and external to the agent layer.
    """

    def __init__(self, profile: dict[str, float], lang: str = DEFAULT_LANG) -> None:
        self.profile = profile
        self.lang = lang

    def _t(self, zh: str, en: str) -> str:
        return t(self.lang, zh, en)

    def run(
        self,
        *,
        symbol: str,
        name: str,
        score: float,
        signals: dict[str, float],
        evidence_ids: list[str],
        quant_view: dict[str, Any] | None = None,
    ) -> TradingAgentsReport:
        analysts = [
            self._technical_analyst(symbol, signals, evidence_ids),
            self._quant_analyst(symbol, score, quant_view),
            self._fundamental_analyst(symbol),
            self._news_analyst(symbol),
            self._sentiment_analyst(symbol),
        ]
        debate = self._research_debate(symbol, analysts)
        trader = self._trader(symbol, name, score, signals, debate)
        risk_reviews = self._risk_guardians(symbol, signals, trader)
        approved = all(r.verdict != "reject" for r in risk_reviews)
        sizing_adjustment = min((r.adjustment for r in risk_reviews), default=1.0)
        fund_manager = {
            "verdict": "approve" if approved else "reject",
            "reason": (
                self._t("多角色报告允许进入外部硬风控",
                        "multi-role report clears the way to the external hard risk engine")
                if approved else
                self._t("角色层风险守卫拒绝进入下单链路",
                        "role-layer risk guardian blocked entry to the order chain")
            ),
            "sizing_adjustment": sizing_adjustment,
            "hard_risk_engine_required": True,
        }
        return TradingAgentsReport(
            analysts=analysts,
            debate=debate,
            trader_plan=trader,
            risk_reviews=risk_reviews,
            fund_manager=fund_manager,
        )

    def _technical_analyst(
        self, symbol: str, signals: dict[str, float], evidence_ids: list[str]
    ) -> AgentReport:
        bullish: list[str] = []
        bearish: list[str] = []
        mom5 = self._t(f"5日动量 {signals.get('mom_5d', 0.0):+.2%}",
                       f"5-day momentum {signals.get('mom_5d', 0.0):+.2%}")
        mom20 = self._t(f"20日动量 {signals.get('mom_20d', 0.0):+.2%}",
                        f"20-day momentum {signals.get('mom_20d', 0.0):+.2%}")
        if signals.get("mom_5d", 0.0) > 0.02:
            bullish.append(mom5)
        elif signals.get("mom_5d", 0.0) < -0.02:
            bearish.append(mom5)
        if signals.get("mom_20d", 0.0) > 0.05:
            bullish.append(mom20)
        elif signals.get("mom_20d", 0.0) < -0.05:
            bearish.append(mom20)
        if signals.get("price_vs_ma20", 0.0) > 0:
            bullish.append(self._t(f"价格高于20日均线 {signals['price_vs_ma20']:+.2%}",
                                   f"price above the 20-day MA by {signals['price_vs_ma20']:+.2%}"))
        elif signals.get("price_vs_ma20", 0.0) < 0:
            bearish.append(self._t(f"价格低于20日均线 {signals['price_vs_ma20']:+.2%}",
                                   f"price below the 20-day MA by {signals['price_vs_ma20']:+.2%}"))

        stance = "bullish" if len(bullish) > len(bearish) else "bearish" if len(bearish) > len(bullish) else "neutral"
        confidence = min(0.9, 0.45 + 0.12 * abs(len(bullish) - len(bearish)))
        observations = bullish + bearish
        return AgentReport(
            role="technical_analyst",
            stance=stance,
            confidence=confidence,
            summary=self._t(
                f"{symbol} 技术面为 {stance}：{'; '.join(observations) or '方向信号不明显'}",
                f"{symbol} technicals read {stance}: "
                f"{'; '.join(observations) or 'no clear directional signal'}"),
            evidence_ids=evidence_ids,
            observations=observations,
            risks=[self._t("技术信号在震荡市中容易反复",
                           "technical signals whipsaw in range-bound markets")] if stance != "neutral" else [],
        )

    def _quant_analyst(
        self, symbol: str, score: float, quant_view: dict[str, Any] | None
    ) -> AgentReport:
        if not quant_view:
            stance = "bullish" if score >= self.profile["buy_score_threshold"] else "neutral"
            return AgentReport(
                role="quant_analyst",
                stance=stance,
                confidence=min(0.85, max(0.2, score)),
                summary=self._t(
                    f"{symbol} 暂无ACTIVE量化模型视图，使用候选评分 {score:.2f}",
                    f"{symbol} has no ACTIVE quant model view; using candidate score {score:.2f}"),
                observations=[f"heuristic_score={score:.2f}"],
                status="degraded",
            )
        rank = quant_view.get("rank")
        universe = quant_view.get("universe_size")
        pred = quant_view.get("predicted_return")
        stance = "bullish" if rank and universe and rank <= max(1, int(universe) // 3) else "neutral"
        if pred is not None and pred < 0:
            stance = "bearish"
        return AgentReport(
            role="quant_analyst",
            stance=stance,
            confidence=float(quant_view.get("confidence") or quant_view.get("probability_up") or 0.6),
            summary=self._t(
                f"{symbol} 量化排名 {rank}/{universe}，预期收益 {pred if pred is not None else 'n/a'}",
                f"{symbol} quant rank {rank}/{universe}, predicted return "
                f"{pred if pred is not None else 'n/a'}"),
            observations=[f"rank={rank}/{universe}", f"model={quant_view.get('model') or quant_view.get('model_id', '')}"],
            status="available",
        )

    def _fundamental_analyst(self, symbol: str) -> AgentReport:
        return AgentReport(
            role="fundamental_analyst",
            stance="neutral",
            confidence=0.0,
            summary=self._t(
                f"{symbol} 基本面数据未接入本交易循环，不参与方向判断",
                f"{symbol} fundamental data is not wired into this trading loop; it takes no part "
                f"in the directional call"),
            status="unavailable",
            risks=[self._t("缺少估值、盈利、资产负债和指引数据",
                           "no valuation, earnings, balance-sheet or guidance data")],
        )

    def _news_analyst(self, symbol: str) -> AgentReport:
        return AgentReport(
            role="news_analyst",
            stance="neutral",
            confidence=0.0,
            summary=self._t(f"{symbol} 新闻/公告实时源未接入本交易循环",
                            f"{symbol} live news/filing feeds are not wired into this trading loop"),
            status="unavailable",
            risks=[self._t("可能漏掉公告、监管、宏观和突发事件",
                           "filings, regulatory, macro and breaking events may be missed")],
        )

    def _sentiment_analyst(self, symbol: str) -> AgentReport:
        return AgentReport(
            role="sentiment_analyst",
            stance="neutral",
            confidence=0.0,
            summary=self._t(f"{symbol} 社媒/市场情绪源未接入本交易循环",
                            f"{symbol} social/sentiment feeds are not wired into this trading loop"),
            status="unavailable",
            risks=[self._t("缺少情绪拥挤度和散户行为验证",
                           "no sentiment-crowding or retail-behaviour corroboration")],
        )

    def _research_debate(self, symbol: str, analysts: list[AgentReport]) -> ResearchDebate:
        bullish_reports = [a for a in analysts if a.stance == "bullish"]
        bearish_reports = [a for a in analysts if a.stance == "bearish"]
        bull_conf = sum(a.confidence for a in bullish_reports) / max(len(bullish_reports), 1)
        bear_conf = sum(a.confidence for a in bearish_reports) / max(len(bearish_reports), 1)
        unavailable = [a.role for a in analysts if a.status == "unavailable"]
        conflict = min(1.0, min(bull_conf, bear_conf) + 0.15 * min(len(bullish_reports), len(bearish_reports)))
        if bull_conf > bear_conf + 0.15:
            consensus = "bullish"
        elif bear_conf > bull_conf + 0.15:
            consensus = "bearish"
        else:
            consensus = "neutral"
        return ResearchDebate(
            bull=AgentReport(
                role="bull_researcher",
                stance="bullish",
                confidence=round(bull_conf, 4),
                summary=self._t(
                    f"{symbol} 多头研究员采纳 {len(bullish_reports)} 个看多报告",
                    f"{symbol} bull researcher adopted {len(bullish_reports)} bullish report(s)"),
                observations=[a.summary for a in bullish_reports],
            ),
            bear=AgentReport(
                role="bear_researcher",
                stance="bearish",
                confidence=round(bear_conf, 4),
                summary=self._t(
                    f"{symbol} 空头研究员采纳 {len(bearish_reports)} 个看空报告，"
                    f"并标记缺口：{', '.join(unavailable) or '无'}",
                    f"{symbol} bear researcher adopted {len(bearish_reports)} bearish report(s) "
                    f"and flagged gaps: {', '.join(unavailable) or 'none'}"),
                observations=[a.summary for a in bearish_reports],
                risks=[r for a in analysts for r in a.risks],
            ),
            consensus=consensus,
            conflict_level=round(conflict, 4),
            balance_note=self._t(
                "多空辩论基于已接入数据；未接入角色只作为风险缺口，不伪造结论",
                "The debate runs on connected data only; unconnected roles are recorded as risk gaps "
                "rather than fabricated into conclusions"),
        )

    def _trader(
        self,
        symbol: str,
        name: str,
        score: float,
        signals: dict[str, float],
        debate: ResearchDebate,
    ) -> TraderPlan:
        action = "buy" if debate.consensus == "bullish" and score >= self.profile["buy_score_threshold"] else "no_action"
        confidence = max(0.1, min(0.9, score * (1.0 - debate.conflict_level * 0.35)))
        base = self.profile["base_target_weight"]
        target = min(self.profile["max_position_weight"], base * (0.5 + confidence))
        why_now = [
            self._t(f"候选评分 {score:.2f} / 阈值 {self.profile['buy_score_threshold']:.2f}",
                    f"candidate score {score:.2f} vs threshold {self.profile['buy_score_threshold']:.2f}"),
            self._t(f"多空共识 {debate.consensus}，冲突 {debate.conflict_level:.2f}",
                    f"bull/bear consensus {debate.consensus}, conflict {debate.conflict_level:.2f}"),
        ]
        if signals.get("volume_ratio", 0.0) >= 2.0:
            why_now.append(self._t(f"量比 {signals['volume_ratio']:.1f}x",
                                   f"volume ratio {signals['volume_ratio']:.1f}x"))
        return TraderPlan(
            action_hint=action,
            target_weight_hint=round(target, 4),
            confidence=round(confidence, 4),
            thesis=self._t(
                f"TradingAgents 交易员计划：{name}({symbol}) 当前共识为 {debate.consensus}，"
                f"候选评分 {score:.2f}，建议 {action}。",
                f"TradingAgents trader plan: {name} ({symbol}) consensus is {debate.consensus}, "
                f"candidate score {score:.2f}, suggested action {action}.",
            ),
            why_now=why_now,
            invalidation_conditions=[
                self._t("多空共识转为 bearish", "consensus flips to bearish"),
                self._t("技术趋势跌破20日均线且短期动量转负",
                        "trend breaks below the 20-day MA and short-term momentum turns negative"),
                self._t("外部风控引擎拒绝或降仓",
                        "the external risk engine rejects or downsizes the position"),
            ],
        )

    def _risk_guardians(
        self, symbol: str, signals: dict[str, float], trader: TraderPlan
    ) -> list[RiskGuardianReview]:
        reviews = [
            RiskGuardianReview(
                name="aggressive_risk_guardian",
                verdict="approve" if trader.action_hint != "no_action" else "pass",
                reason=self._t(f"{symbol} 只允许进入外部硬风控，不直接执行",
                               f"{symbol} may only proceed to the external hard risk engine, "
                               f"never straight to execution"),
                max_weight=self.profile["max_position_weight"],
            )
        ]
        vol = signals.get("volatility_20d", 0.0)
        if vol > 0.03:
            reviews.append(RiskGuardianReview(
                name="conservative_risk_guardian",
                verdict="downsize",
                reason=self._t(f"20日波动率 {vol:.2%} 偏高，角色层建议目标仓位打八折",
                               f"20-day volatility {vol:.2%} is elevated; the role layer suggests "
                               f"cutting the target size to 80%"),
                max_weight=self.profile["max_position_weight"] * 0.8,
                adjustment=0.8,
            ))
        else:
            reviews.append(RiskGuardianReview(
                name="conservative_risk_guardian",
                verdict="approve",
                reason=self._t("未发现角色层波动率降仓触发条件",
                               "no role-layer volatility downsizing trigger was met"),
                max_weight=self.profile["max_position_weight"],
            ))
        return reviews
