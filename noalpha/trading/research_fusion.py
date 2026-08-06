"""Phase Q5: Evidence-grounded LLM research → structured trading signals → fusion.

Reuses the EXISTING investigation pipeline end to end (evidence, PIT guard,
claims, citation validation, bull/bear/critic, deterministic confidence) —
no new "you are a trader" prompt exists anywhere in this module. The trading
loop consumes a structured TradingResearchSnapshot, never report markdown,
and the LLM never places orders: its signal enters a versioned deterministic
fusion policy, then portfolio/risk.

Latency/cost control: top-K gating, freshness reuse, and a hard call budget.
LLM failure degrades to QUANT_ONLY_FALLBACK visibly — it never crashes the
loop and never silently pretends to be hybrid.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..i18n import DEFAULT_LANG, t

logger = logging.getLogger(__name__)

FUSION_POLICY_VERSION = "fusion-policy:v1"
SNAPSHOT_SCHEMA_VERSION = "trading-research-snapshot:v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchDirection(str, Enum):
    STRONGLY_BULLISH = "strongly_bullish"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    STRONGLY_BEARISH = "strongly_bearish"
    UNCERTAIN = "uncertain"          # first-class: bull AND bear both strong


class ConflictState(str, Enum):
    NONE = "none"
    QUANT_BULL_LLM_BEAR = "quant_bull_llm_bear"
    QUANT_BEAR_LLM_BULL = "quant_bear_llm_bull"
    HIGH_INTERNAL_LLM_CONFLICT = "high_internal_llm_conflict"
    LOW_EVIDENCE = "low_evidence"
    MISSING_LLM = "missing_llm"


@dataclass
class TradingResearchSnapshot:
    """Structured, PIT-annotated view of one real investigation.

    Field semantics (第七条):
      bull_strength   0.0 = no evidence-supported bullish thesis;
                      1.0 = strong multi-source supported bullish thesis.
                      Taken from the bull-case pass confidence, NOT sentiment.
      bear_strength   independent of bull (NOT 1-bull); both high = uncertainty.
      thesis_confidence  the DETERMINISTIC confidence engine output
                      (evidence quality + agreement + unknowns + citations).
      citation_support   share of LLM claims whose citations actually resolve
                      to real evidence ids (0.0 happens and is recorded as-is).
      contradiction_score  overlap of strong bull & bear + contradictory claims
                      + high-severity critic findings.
      unknown_risk_score   density of open unknowns.
    """
    snapshot_id: str = field(default_factory=lambda: f"trs-{uuid.uuid4().hex[:12]}")
    schema_version: str = SNAPSHOT_SCHEMA_VERSION
    instrument_id: str = ""
    symbol: str = ""
    market: str = ""
    as_of: str = ""
    generated_at: str = ""
    available_at: str = ""            # = real completion time; never earlier
    investigation_id: str = ""
    research_mode: str = ""
    llm_provider: str = ""
    model: str = ""
    prompt_versions: dict[str, str] = field(default_factory=dict)
    context_id: str = ""
    # scores
    bull_strength: float = 0.0
    bear_strength: float = 0.0
    thesis_confidence: float = 0.0
    evidence_quality: float = 0.0
    citation_support: float = 0.0
    event_materiality: float = 0.0
    positive_catalyst_score: float = 0.0
    negative_risk_score: float = 0.0
    contradiction_score: float = 0.0
    unknown_risk_score: float = 0.0
    # view
    thesis_direction: ResearchDirection = ResearchDirection.NEUTRAL
    thesis_status: str = "active"
    expected_horizon: str = "5-20 trading days"
    bull_thesis: str = ""
    bear_thesis: str = ""
    key_claim_ids: list[str] = field(default_factory=list)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    selected_evidence_count: int = 0
    # provenance / cost
    provider_health: dict[str, str] = field(default_factory=dict)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    # PIT flags (第一条): forward-generated live snapshots are pit_safe;
    # any retro-generated snapshot MUST be research_only and never trainable
    pit_safe: bool = True
    research_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["thesis_direction"] = self.thesis_direction.value
        return d


@dataclass
class LLMResearchSignal:
    signal_id: str = field(default_factory=lambda: f"lsig-{uuid.uuid4().hex[:12]}")
    instrument_id: str = ""
    symbol: str = ""
    as_of: str = ""
    direction: ResearchDirection = ResearchDirection.NEUTRAL
    strength: float = 0.0             # |bull - bear|, 0..1
    confidence: float = 0.0           # thesis_confidence damped by citation support
    risk_score: float = 0.0
    conflict_score: float = 0.0
    horizon: str = "5-20 trading days"
    snapshot_id: str = ""
    low_evidence: bool = False
    pit_safe: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction.value
        return d


def extract_snapshot(result: Any, *, secid: str, symbol: str, market: str,
                     llm_provider: str, model: str, latency_ms: float,
                     input_tokens: int, output_tokens: int) -> TradingResearchSnapshot:
    """Deterministic extraction from a real InvestigationResult — no extra
    LLM call, no fabricated fields: everything maps to pipeline output."""
    report = result.report
    q = report.quality_report or {}
    cases = q.get("llm_cases") or {}
    citation = q.get("citation_support") or {}
    llm_ctx = q.get("llm_context") or {}
    breakdown = getattr(report, "confidence_breakdown", None) or {}

    bull = float(cases.get("bull_confidence") or 0.0)
    bear = float(cases.get("bear_confidence") or 0.0)
    citation_rate = float(citation.get("citation_support_rate") or 0.0)
    claim_count = int(citation.get("claim_count") or 0)
    contradictory = int(citation.get("contradictory_claims") or 0)
    critic_sev = [s.lower() for s in (cases.get("critic_severities") or [])]
    high_critic = sum(1 for s in critic_sev if s in ("high", "critical"))

    modality_keys = ("market_data", "fundamentals", "filings", "news", "temporal_integrity")
    modality_scores = [float(breakdown[k]) for k in modality_keys if k in breakdown]
    evidence_quality = sum(modality_scores) / len(modality_scores) if modality_scores else 0.5

    contradiction = min(1.0, max(0.0, (min(bull, bear) * 2.0)
                                 + (contradictory / claim_count * 0.5 if claim_count else 0.0)
                                 + 0.1 * high_critic))
    unknowns = list(getattr(report, "unknowns", []) or [])
    unknown_risk = min(1.0, len(unknowns) / 12.0)
    events = list(getattr(report, "detected_events", []) or [])
    event_materiality = min(1.0, 0.25 * len(events))

    net = bull - bear
    if bull >= 0.55 and bear >= 0.55:
        direction = ResearchDirection.UNCERTAIN
    elif net >= 0.45:
        direction = ResearchDirection.STRONGLY_BULLISH
    elif net >= 0.15:
        direction = ResearchDirection.BULLISH
    elif net <= -0.45:
        direction = ResearchDirection.STRONGLY_BEARISH
    elif net <= -0.15:
        direction = ResearchDirection.BEARISH
    else:
        direction = ResearchDirection.NEUTRAL

    now = _now().isoformat()
    as_of = getattr(report, "as_of", None)
    claims = list(getattr(report, "claims", []) or [])
    return TradingResearchSnapshot(
        instrument_id=secid, symbol=symbol, market=market,
        as_of=(as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of or now)),
        generated_at=now, available_at=now,   # available only once actually done (第二十九条)
        investigation_id=result.investigation_id,
        research_mode="llm", llm_provider=llm_provider, model=model,
        prompt_versions={"version": "v1"},
        context_id=str(llm_ctx.get("context_id") or ""),
        bull_strength=bull, bear_strength=bear,
        thesis_confidence=float(getattr(report, "confidence", 0.0) or 0.0),
        evidence_quality=round(evidence_quality, 4),
        citation_support=citation_rate,
        event_materiality=round(event_materiality, 4),
        positive_catalyst_score=round(min(1.0, int(cases.get("bull_catalysts") or 0) / 5.0) * bull, 4),
        negative_risk_score=round(max(min(1.0, int(cases.get("bear_drivers") or 0) / 5.0) * bear,
                                      min(1.0, 0.25 * high_critic)), 4),
        contradiction_score=round(contradiction, 4),
        unknown_risk_score=round(unknown_risk, 4),
        thesis_direction=direction,
        bull_thesis=str(cases.get("bull_thesis") or ""),
        bear_thesis=str(cases.get("bear_thesis") or ""),
        key_claim_ids=[c.claim_id for c in claims[:8]],
        supporting_evidence_ids=list(getattr(report, "evidence_refs", []) or []),
        invalidation_conditions=list(cases.get("bull_invalidation") or []),
        selected_evidence_count=int(llm_ctx.get("selected_count") or 0),
        provider_health={k: str((v or {}).get("status", "")) for k, v in
                         (q.get("provider_health") or {}).items() if isinstance(v, dict)},
        latency_ms=latency_ms, input_tokens=input_tokens, output_tokens=output_tokens,
        pit_safe=True, research_only=False,
    )


def snapshot_to_signal(snap: TradingResearchSnapshot) -> LLMResearchSignal:
    low_evidence = snap.selected_evidence_count < 8 or snap.citation_support < 0.1
    # confidence: deterministic thesis confidence, damped when claims are not
    # actually citation-backed (a model score is not a probability, 第四十九条)
    confidence = snap.thesis_confidence * (0.5 + 0.5 * snap.citation_support)
    return LLMResearchSignal(
        instrument_id=snap.instrument_id, symbol=snap.symbol, as_of=snap.as_of,
        direction=snap.thesis_direction,
        strength=round(abs(snap.bull_strength - snap.bear_strength), 4),
        confidence=round(confidence, 4),
        risk_score=snap.negative_risk_score,
        conflict_score=snap.contradiction_score,
        horizon=snap.expected_horizon,
        snapshot_id=snap.snapshot_id,
        low_evidence=low_evidence,
        pit_safe=snap.pit_safe,
    )


# ── fusion ──


@dataclass
class FusionDecision:
    decision_id: str = field(default_factory=lambda: f"fus-{uuid.uuid4().hex[:12]}")
    instrument_id: str = ""
    symbol: str = ""
    as_of: str = field(default_factory=lambda: _now().isoformat())
    quant_signal_id: str = ""
    llm_signal_id: str = ""
    quant_contribution: dict[str, Any] = field(default_factory=dict)
    llm_contribution: dict[str, Any] = field(default_factory=dict)
    conflict_state: ConflictState = ConflictState.NONE
    final_signal: str = "no_action"   # buy | hold | reduce | sell | no_action | watch
    final_strength: float = 0.0       # size multiplier applied to quant alpha target
    effective_mode: str = "hybrid_llm_quant"   # or quant_only_fallback / deterministic
    explanation: str = ""
    policy_version: str = FUSION_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["conflict_state"] = self.conflict_state.value
        return d


BULLISH_DIRS = {ResearchDirection.BULLISH, ResearchDirection.STRONGLY_BULLISH}
BEARISH_DIRS = {ResearchDirection.BEARISH, ResearchDirection.STRONGLY_BEARISH}


class FusionPolicy:
    """v1 deterministic fusion (第十三/十四条): quant discovers candidates and
    sets expected return; the LLM confirms, contextualizes or vetoes. The LLM
    alone can never force a large position."""

    def __init__(self, lang: str = DEFAULT_LANG) -> None:
        self.lang = lang

    def _t(self, zh: str, en: str) -> str:
        return t(self.lang, zh, en)

    def fuse(self, *, symbol: str, secid: str, quant: Any | None,
             llm: LLMResearchSignal | None, holding: bool = False) -> FusionDecision:
        d = FusionDecision(instrument_id=secid, symbol=symbol)
        quant_long = bool(quant and getattr(quant.direction, "value", "") == "long")
        quant_short = bool(quant and getattr(quant.direction, "value", "") == "short")
        quant_strength = float(getattr(quant, "strength", 0.0) or 0.0)
        d.quant_signal_id = getattr(quant, "signal_id", "") if quant else ""
        d.quant_contribution = ({"direction": quant.direction.value, "strength": quant_strength,
                                 "confidence": quant.confidence, "calibrated": quant.calibrated,
                                 "model": quant.model_version} if quant else {"missing": True})

        if llm is None:
            d.conflict_state = ConflictState.MISSING_LLM
            d.effective_mode = "quant_only_fallback"
            d.llm_contribution = {"missing": True}
            if holding:
                d.final_signal, d.final_strength = ("reduce", 0.5) if quant_short else ("hold", 1.0)
                d.explanation = self._t("LLM 信号不可用；持仓管理退化为 quant-only 规则",
                                 "LLM signal unavailable; position management falls back to quant-only rules")
            elif quant_long and quant_strength >= 0.6:
                d.final_signal, d.final_strength = "buy", 0.75
                d.explanation = self._t("LLM 信号不可用（QUANT_ONLY_FALLBACK）：按量化信号建仓但降低目标 25%",
                                 "LLM signal unavailable (QUANT_ONLY_FALLBACK): entering on the quant signal at a 25% reduced target")
            else:
                d.final_signal = "no_action"
                d.explanation = self._t("LLM 信号不可用且量化信号不足以单独驱动",
                                 "LLM signal unavailable and the quant signal is too weak to drive alone")
            return d

        d.llm_signal_id = llm.signal_id
        d.llm_contribution = llm.to_dict()

        if llm.direction == ResearchDirection.UNCERTAIN:
            d.conflict_state = ConflictState.HIGH_INTERNAL_LLM_CONFLICT
            if holding:
                d.final_signal, d.final_strength = "reduce", 0.5
                d.explanation = self._t("LLM bull/bear 同时强（内部高冲突）：持仓减半，等待证据收敛",
                                 "LLM is strongly bullish and bearish at once (high internal conflict): halving the position until the evidence converges")
            elif quant_long and quant_strength >= 0.6:
                d.final_signal, d.final_strength = "buy", 0.5
                d.explanation = self._t("量化强但 LLM 高度不确定：仅允许半仓试探",
                                 "Quant is strong but the LLM is highly uncertain: a half-size probe only")
            else:
                d.final_signal = "no_action"
                d.explanation = self._t("LLM 内部高冲突且量化不强：观望",
                                 "High internal LLM conflict and a weak quant signal: stand aside")
            return d

        if llm.low_evidence:
            d.conflict_state = ConflictState.LOW_EVIDENCE
            # low-evidence LLM must not veto a strong quant signal, but it
            # cannot confirm either — proceed at reduced size
            if holding:
                d.final_signal, d.final_strength = ("reduce", 0.5) if quant_short else ("hold", 1.0)
                d.explanation = self._t("LLM 证据不足（citation/selection 低）：持仓按量化规则管理",
                                 "Insufficient LLM evidence (low citation/selection): manage the position by quant rules")
            elif quant_long and quant_strength >= 0.6:
                d.final_signal, d.final_strength = "buy", 0.75
                d.explanation = self._t(
                    f"LLM 证据薄弱（citation-damped conf {llm.confidence:.2f}）"
                    f"，不足以确认或否决：按量化信号 75% 目标执行",
                    f"Weak LLM evidence (citation-damped conf {llm.confidence:.2f}) — enough to neither "
                    f"confirm nor veto: execute the quant signal at 75% of target")
            else:
                d.final_signal = "no_action"
                d.explanation = self._t("证据不足且量化不强：观望",
                                 "Insufficient evidence and a weak quant signal: stand aside")
            return d

        llm_bull = llm.direction in BULLISH_DIRS
        llm_bear = llm.direction in BEARISH_DIRS

        if quant_long and llm_bear:
            d.conflict_state = ConflictState.QUANT_BULL_LLM_BEAR
            strong_bear = llm.direction == ResearchDirection.STRONGLY_BEARISH or llm.risk_score >= 0.5
            if holding:
                d.final_signal, d.final_strength = ("sell", 0.0) if strong_bear else ("reduce", 0.5)
                d.explanation = self._t(
                    f"持仓冲突：量化看多但 LLM 看空（risk {llm.risk_score:.2f}）"
                    f"→ {'清仓' if strong_bear else '减半'}",
                    f"Conflict on a held name: quant bullish but LLM bearish (risk {llm.risk_score:.2f})"
                    f" → {'exit' if strong_bear else 'halve'}")
            else:
                d.final_signal = "no_action" if strong_bear else "buy"
                d.final_strength = 0.0 if strong_bear else 0.5
                d.explanation = (
                    self._t("量化看多 vs LLM 强看空：否决建仓（NO_ACTION）",
                            "Quant bullish vs. strongly bearish LLM: entry vetoed (NO_ACTION)")
                    if strong_bear else
                    self._t("量化看多 vs LLM 温和看空：半仓，冲突已记录",
                            "Quant bullish vs. mildly bearish LLM: half size, conflict recorded"))
            return d

        if quant_short and llm_bull:
            d.conflict_state = ConflictState.QUANT_BEAR_LLM_BULL
            if holding:
                d.final_signal, d.final_strength = "reduce", 0.5
                d.explanation = self._t("量化排名恶化但 LLM 看多：减半而非清仓，等待下一轮证据",
                                 "Quant rank deteriorated but the LLM is bullish: halve rather than exit, pending the next round of evidence")
            else:
                d.final_signal = "no_action"
                d.explanation = self._t("量化弱但 LLM 看多：v1 策略不允许 LLM 单独开仓（观望/watch）",
                                 "Quant is weak while the LLM is bullish: the v1 policy never lets the LLM open a position alone (watch)")
            return d

        if quant_long and llm_bull:
            strong = (llm.direction == ResearchDirection.STRONGLY_BULLISH
                      and llm.confidence >= 0.5 and llm.conflict_score < 0.3)
            d.final_signal = "buy" if not holding else "hold"
            d.final_strength = 1.15 if strong else 1.0
            d.explanation = self._t(
                f"CONFIRMED：量化 {quant_strength:.2f} + LLM {llm.direction.value}"
                f"（conf {llm.confidence:.2f}，conflict {llm.conflict_score:.2f}）"
                + ("，强确认放大 15%" if strong else ""),
                f"CONFIRMED: quant {quant_strength:.2f} + LLM {llm.direction.value} "
                f"(conf {llm.confidence:.2f}, conflict {llm.conflict_score:.2f})"
                + (", strong confirmation scaled up 15%" if strong else ""))
            return d

        if quant_long and llm.direction == ResearchDirection.NEUTRAL:
            d.final_signal = "buy" if not holding else "hold"
            d.final_strength = 0.85
            d.explanation = self._t("量化看多，LLM 中性（无实质性反证）：85% 目标执行",
                                 "Quant bullish, LLM neutral (no material counter-evidence): execute at 85% of target")
            return d

        if not quant_long and llm_bull:
            # LLM alone cannot open a large position (第十四条)
            d.final_signal, d.final_strength = "watch", 0.0
            d.explanation = self._t("LLM 看多但量化未确认：加入观察，不开仓",
                                 "LLM bullish but unconfirmed by quant: add to watch, do not open")
            return d

        if holding and (quant_short or llm_bear):
            d.final_signal, d.final_strength = "reduce", 0.5
            d.explanation = self._t("持仓的量化或 LLM 观点转弱：减半",
                                 "Quant or LLM view on the held name weakened: halve")
            return d

        d.final_signal = "hold" if holding else "no_action"
        d.explanation = self._t("量化与 LLM 均无足够强的方向信号",
                                 "Neither quant nor LLM offers a strong enough directional signal")
        return d


# ── freshness / budget ──


@dataclass
class ResearchFreshnessPolicy:
    max_age_hours: float = 6.0
    material_day_move_pct: float = 5.0

    def is_fresh(self, snap: TradingResearchSnapshot, *, now: datetime | None = None,
                 day_change_pct: float = 0.0, quant_turned_short: bool = False) -> bool:
        now = now or _now()
        try:
            age_h = (now - datetime.fromisoformat(snap.generated_at)).total_seconds() / 3600
        except ValueError:
            return False
        if age_h > self.max_age_hours:
            return False
        # material triggers force refresh even when recent
        if abs(day_change_pct) >= self.material_day_move_pct:
            return False
        if quant_turned_short and snap.thesis_direction in BULLISH_DIRS:
            return False
        return True


class LLMCallBudget:
    def __init__(self, max_calls_per_cycle: int = 1, max_calls_per_hour: int = 6,
                 max_tokens_per_day: int = 500_000, high_severity_bypass: bool = True) -> None:
        self.max_calls_per_cycle = max_calls_per_cycle
        self.max_calls_per_hour = max_calls_per_hour
        self.max_tokens_per_day = max_tokens_per_day
        self.high_severity_bypass = high_severity_bypass
        self.cycle_calls = 0
        self._hour_marks: list[float] = []
        self._day_tokens = 0
        self._day_key = ""

    def new_cycle(self) -> None:
        self.cycle_calls = 0

    def allow(self, *, high_severity: bool = False) -> tuple[bool, str]:
        now = time.time()
        self._hour_marks = [t for t in self._hour_marks if now - t < 3600]
        day_key = datetime.now(timezone.utc).date().isoformat()
        if day_key != self._day_key:
            self._day_key, self._day_tokens = day_key, 0
        if self.cycle_calls >= self.max_calls_per_cycle:
            if not (high_severity and self.high_severity_bypass):
                return False, f"cycle budget exhausted ({self.max_calls_per_cycle})"
        if len(self._hour_marks) >= self.max_calls_per_hour:
            return False, f"hourly budget exhausted ({self.max_calls_per_hour})"
        if self._day_tokens >= self.max_tokens_per_day:
            return False, f"daily token budget exhausted ({self.max_tokens_per_day})"
        # Consume the slot immediately — failure does not refund it.
        # Otherwise a failing provider (timeout/rate-limit/500) would burn
        # through the entire universe before the budget cap kicks in.
        self.cycle_calls += 1
        self._hour_marks.append(time.time())
        return True, ""

    def record(self, tokens: int) -> None:
        """Track token usage only (attempt was already consumed in allow())."""
        self._day_tokens += tokens


# ── the service the trading loop talks to ──


class TradingResearchService:
    """Runs REAL investigations for the trading loop with freshness + budget.

    pipeline_factory: () -> InvestigationPipeline in LLM mode. Kept lazy so a
    missing key or provider outage shows up as LLM_SIGNAL_UNAVAILABLE, not an
    import-time crash.
    """

    def __init__(self, pipeline_factory, *, model: str, llm_provider: str,
                 budget: LLMCallBudget | None = None,
                 freshness: ResearchFreshnessPolicy | None = None,
                 feature_store: Any | None = None,
                 lookback_days: int = 30) -> None:
        self.pipeline_factory = pipeline_factory
        self.model = model
        self.llm_provider = llm_provider
        self.budget = budget or LLMCallBudget()
        self.freshness = freshness or ResearchFreshnessPolicy()
        self.feature_store = feature_store
        self.lookback_days = lookback_days
        self.cache: dict[str, TradingResearchSnapshot] = {}
        self.last_error = ""
        self._pipeline = None

    def _get_pipeline(self):
        if self._pipeline is None:
            self._pipeline = self.pipeline_factory()
        return self._pipeline

    def research(self, *, secid: str, symbol: str, market: str,
                 day_change_pct: float = 0.0, quant_turned_short: bool = False,
                 holding: bool = False) -> tuple[TradingResearchSnapshot | None, str]:
        """Returns (snapshot, status). status ∈ fresh_reuse | generated |
        budget_denied:* | failed:*"""
        cached = self.cache.get(secid)
        if cached is not None and self.freshness.is_fresh(
                cached, day_change_pct=day_change_pct, quant_turned_short=quant_turned_short):
            return cached, "fresh_reuse"
        ok, why = self.budget.allow(high_severity=holding and quant_turned_short)
        if not ok:
            return cached, f"budget_denied:{why}"
        try:
            pipeline = self._get_pipeline()
            t0 = time.time()
            result = pipeline.investigate(symbol, lookback_days=self.lookback_days,
                                          report_language="zh-CN")
            latency_ms = (time.time() - t0) * 1000
            usage = getattr(getattr(pipeline.research, "provider", None), "usage", None)
            snap = extract_snapshot(
                result, secid=secid, symbol=symbol, market=market,
                llm_provider=self.llm_provider, model=self.model,
                latency_ms=latency_ms,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0)
            self.budget.record(snap.input_tokens + snap.output_tokens)
            self.cache[secid] = snap
            if self.feature_store is not None:
                try:
                    self.feature_store.append_from_trading_snapshot(snap)
                except Exception:
                    logger.exception("LLM feature persistence failed (non-fatal)")
            return snap, "generated"
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("LLM research failed for %s: %s", symbol, exc)
            return None, f"failed:{type(exc).__name__}: {exc}"


def default_trading_research_service(state_dir: str | Path = ".cache/api",
                                     feature_store: Any | None = None) -> TradingResearchService | None:
    """Builds the service from the SAME credential store + investigation
    pipeline the API uses. Returns None when no key is configured — callers
    then run quant-only and say so. Imports are function-local to avoid a
    circular import with noalpha.api.

    The trading loop defaults to the faster flash-tier model (a config choice,
    not a different prompt): the pro-tier model measured >7 min per
    investigation on this machine, flash ≈160 s. Override with the
    LLM_TRADING_MODEL credential/env key.
    """
    import os
    from dataclasses import replace as dc_replace
    from noalpha.api import CredentialStore, resolve_llm_config
    from noalpha.pipeline import InvestigationPipeline
    from noalpha.research_mode import ResearchMode

    creds = CredentialStore(Path(state_dir) / "credentials.json")
    cfg = resolve_llm_config(creds)
    if not cfg.api_key:
        return None
    trading_model = (creds.get("LLM_TRADING_MODEL") or os.environ.get("LLM_TRADING_MODEL")
                     or "deepseek-v4-flash")
    cfg = dc_replace(cfg, model=trading_model)

    def factory():
        return InvestigationPipeline.default(
            cache_dir=Path(state_dir) / "providers",
            marketdata_token=creds.get("MARKETDATA_APP_TOKEN") or None,
            research_mode=ResearchMode.LLM, llm_config=cfg)

    return TradingResearchService(factory, model=cfg.model, llm_provider=cfg.base_url,
                                  feature_store=feature_store)
