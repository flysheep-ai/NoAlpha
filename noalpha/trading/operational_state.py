"""Agent operational phases — the full 24/7 lifecycle.

Driven by MarketCalendar, NOT by hardcoded time checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class OperationPhase(str, Enum):
    """What the agent is currently doing — one phase per cycle."""
    INITIALIZING = "initializing"

    # ── pre-market ──
    PRE_MARKET_BRIEF = "pre_market_brief"            # overnight digest + revalidation

    # ── active trading ──
    MARKET_MONITORING = "market_monitoring"          # watching, scanning, but not yet acting
    ACTIVE_TRADING = "active_trading"                # full scan → candidate → research → propose → execute

    # ── mid-session ──
    MIDDAY_MONITORING = "midday_monitoring"          # intraday risk / thesis check, lower frequency

    # ── post-market ──
    POST_MARKET_REVIEW = "post_market_review"        # compute daily report, decision quality analysis

    # ── cross-market / overnight ──
    CROSS_MARKET_MONITORING = "cross_market_monitoring"  # watching open markets, cross-market impact
    OVERNIGHT_RESEARCH = "overnight_research"        # low-frequency monitoring of news, filings, global events

    # ── weekend ──
    WEEKEND_RESEARCH = "weekend_research"            # deeper analysis, replay, strategy evaluation

    # ── terminal ──
    HALTED = "halted"
    DEGRADED = "degraded"  # e.g. provider down but agent still alive


PHASE_LABELS_ZH: dict[OperationPhase, str] = {
    OperationPhase.INITIALIZING: "初始化中",
    OperationPhase.PRE_MARKET_BRIEF: "盘前研究",
    OperationPhase.MARKET_MONITORING: "市场监控",
    OperationPhase.ACTIVE_TRADING: "自主交易中",
    OperationPhase.MIDDAY_MONITORING: "盘中监控",
    OperationPhase.POST_MARKET_REVIEW: "收盘复盘",
    OperationPhase.CROSS_MARKET_MONITORING: "跨市场监控",
    OperationPhase.OVERNIGHT_RESEARCH: "夜间研究",
    OperationPhase.WEEKEND_RESEARCH: "周末研究",
    OperationPhase.HALTED: "已停止",
    OperationPhase.DEGRADED: "降级运行",
}

PHASE_LABELS_EN: dict[OperationPhase, str] = {
    OperationPhase.INITIALIZING: "Initializing",
    OperationPhase.PRE_MARKET_BRIEF: "Pre-Market Brief",
    OperationPhase.MARKET_MONITORING: "Market Monitoring",
    OperationPhase.ACTIVE_TRADING: "Active Trading",
    OperationPhase.MIDDAY_MONITORING: "Midday Check",
    OperationPhase.POST_MARKET_REVIEW: "Post-Market Review",
    OperationPhase.CROSS_MARKET_MONITORING: "Cross-Market Monitoring",
    OperationPhase.OVERNIGHT_RESEARCH: "Overnight Research",
    OperationPhase.WEEKEND_RESEARCH: "Weekend Research",
    OperationPhase.HALTED: "Halted",
    OperationPhase.DEGRADED: "Degraded",
}


@dataclass
class AgentOperationalState:
    phase: OperationPhase = OperationPhase.INITIALIZING
    home_market: str = "CN"
    monitored_markets: list[str] = field(default_factory=lambda: ["CN", "US", "HK"])
    execution_markets: list[str] = field(default_factory=lambda: ["CN"])

    last_phase_change: str = ""
    last_heartbeat: str = ""
    phase_started_at: str = ""

    # current activity
    current_activity: str = ""
    current_research_target: str = ""
    current_research_events: list[str] = field(default_factory=list)

    # counts
    daily_report_count: int = 0
    overnight_event_count: int = 0
    thesis_updates_since_close: int = 0

    def heartbeat(self) -> None:
        self.last_heartbeat = datetime.now(timezone.utc).isoformat()

    def transition(self, phase: OperationPhase, activity: str = "") -> None:
        self.phase = phase
        self.last_phase_change = datetime.now(timezone.utc).isoformat()
        self.phase_started_at = self.last_phase_change
        self.current_activity = activity

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "phase_label_zh": PHASE_LABELS_ZH.get(self.phase, self.phase.value),
            "phase_label_en": PHASE_LABELS_EN.get(self.phase, self.phase.value),
            "home_market": self.home_market,
            "monitored_markets": self.monitored_markets,
            "execution_markets": self.execution_markets,
            "last_phase_change": self.last_phase_change,
            "last_heartbeat": self.last_heartbeat,
            "current_activity": self.current_activity,
            "current_research_target": self.current_research_target,
            "daily_report_count": self.daily_report_count,
            "overnight_event_count": self.overnight_event_count,
            "thesis_updates_since_close": self.thesis_updates_since_close,
        }
