from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from datetime import datetime, timezone

from noalpha.domain import (
    Claim, ClaimType, DetectedEvent, Evidence, EvidenceType,
    Instrument, ResearchReport, TimelineEvent, now_utc, stable_id,
)
from noalpha.llm.provider import LLMProvider
from noalpha.llm.schemas import LLMMessage, LLMUsage
from noalpha.progress import InvestigationStage, ProgressReporter
from noalpha.research.audit import ResearchAuditTrail
from noalpha.research.context_builder import ResearchContextBuilder
from noalpha.research.context_fields import (
    AnalysisResult, BearCaseResult, BullCaseResult, CritiqueFinding,
    CritiqueResult, ResearchContext, ResearchPlan, ResearchQuestion,
    ResearchRunMetadata, ResearchUnknown, ValidatedClaim,
)
from noalpha.confidence import calculate_confidence_breakdown
from noalpha.quality import evaluate_claim_citations

logger = logging.getLogger(__name__)


def _evidence_list_for_llm(selected: list[tuple[Evidence, str]]) -> str:
    lines: list[str] = []
    for item, reason in selected:
        meta_keys = ["symbol", "value", "unit", "concept", "period_start", "period_end",
                      "filing_type", "filing_date", "close", "volume", "open", "high", "low",
                      "importance", "source_observation_id"]
        meta_str = ", ".join(
            f"{k}={item.metadata[k]}" for k in meta_keys if k in item.metadata
        )
        lines.append(
            f"[{item.evidence_id}] ({item.evidence_type.value}) "
            f"source={item.source} time={item.event_time.isoformat()} "
            f"content={item.content[:300]} metadata={{{meta_str}}} "
            f"reason={reason}"
        )
    return "\n".join(lines)


def _events_for_llm(events: tuple[DetectedEvent, ...]) -> str:
    lines: list[str] = []
    for e in events:
        refs = ", ".join(f"[{ref}]" for ref in e.evidence_refs)
        lines.append(
            f"{e.event_type.value}: {e.description} "
            f"(severity={e.severity:.2f}, time={e.event_time.isoformat()}, "
            f"evidence={refs})"
        )
    return "\n".join(lines) if lines else "No events detected."


class LLMResearchOrchestrator:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self.audit = ResearchAuditTrail()

    def build_report(
        self,
        instrument: Instrument,
        context: ResearchContext,
        timeline: tuple[TimelineEvent, ...],
        detected_events: tuple[DetectedEvent, ...],
        evidence: list[Evidence],
        provider_warnings: list[str] | None = None,
        quality_report: dict | None = None,
        progress: ProgressReporter | None = None,
        report_language: str = "en-US",
    ) -> ResearchReport:
        self._report_language = report_language
        started = time.monotonic()
        evidence_text = _evidence_list_for_llm(context.selected_evidence)
        events_text = _events_for_llm(detected_events)
        warnings = provider_warnings or []
        evidence_by_id = {item.evidence_id: item for item in evidence}
        all_evidence_ids = set(evidence_by_id.keys())

        def _emit_start(stage: InvestigationStage, msg: str) -> None:
            if progress:
                progress.stage_started(stage, msg)

        def _emit_done(stage: InvestigationStage, msg: str, **metrics: Any) -> None:
            if progress:
                progress.stage_completed(stage, msg, metrics=metrics if metrics else None)

        # ── Phase 1: Research Plan ──
        _emit_start(InvestigationStage.PLANNING_RESEARCH, "Formulating research questions")
        plan = self._plan(symbol=instrument.symbol, company_name=instrument.company_name,
                          evidence_text=evidence_text, events_text=events_text,
                          unknowns="; ".join(context.unknowns_summary),
                          missing=", ".join(context.missing_modalities) or "none")
        _emit_done(InvestigationStage.PLANNING_RESEARCH,
                   f"{len(plan.questions)} research questions planned",
                   questions=len(plan.questions))

        # ── Phase 2: Analysis Passes ──
        _emit_start(InvestigationStage.ANALYZING_MARKET, "LLM analyzing market data")
        market = self._analyze_market(instrument, evidence_text, events_text, plan)
        _emit_done(InvestigationStage.ANALYZING_MARKET, "Market analysis complete",
                   findings=len(market.key_findings), confidence=market.confidence)

        _emit_start(InvestigationStage.ANALYZING_FUNDAMENTALS, "LLM analyzing financial fundamentals")
        fundamental = self._analyze_fundamentals(instrument, evidence_text, plan)
        _emit_done(InvestigationStage.ANALYZING_FUNDAMENTALS, "Fundamental analysis complete",
                   findings=len(fundamental.key_findings), confidence=fundamental.confidence)

        _emit_start(InvestigationStage.ANALYZING_FILINGS, "LLM analyzing SEC filings")
        filing = self._analyze_filings(instrument, evidence_text, plan)
        _emit_done(InvestigationStage.ANALYZING_FILINGS, "Filing analysis complete",
                   findings=len(filing.key_findings), confidence=filing.confidence)

        _emit_start(InvestigationStage.ANALYZING_NEWS, "LLM analyzing recent news")
        news = self._analyze_news(instrument, evidence_text, events_text, plan)
        _emit_done(InvestigationStage.ANALYZING_NEWS, "News analysis complete",
                   findings=len(news.key_findings), confidence=news.confidence)

        # ── Phase 3: Cross-source ──
        _emit_start(InvestigationStage.CROSS_SOURCE_SYNTHESIS, "LLM synthesizing across sources")
        cross = self._cross_source(instrument, evidence_text, events_text, market, fundamental, filing, news)
        _emit_done(InvestigationStage.CROSS_SOURCE_SYNTHESIS, "Cross-source synthesis complete",
                   findings=len(cross.key_findings), confidence=cross.confidence)

        # ── Phase 4: Bull & Bear ──
        _emit_start(InvestigationStage.BUILDING_BULL_CASE, "LLM constructing bull case")
        bull = self._bull_case(instrument, evidence_text, events_text, market, fundamental, filing, news, cross)
        _emit_done(InvestigationStage.BUILDING_BULL_CASE, bull.thesis[:80] + "…" if len(bull.thesis) > 80 else bull.thesis,
                   confidence=bull.confidence, catalysts=len(bull.catalysts))

        _emit_start(InvestigationStage.BUILDING_BEAR_CASE, "LLM constructing bear case")
        bear = self._bear_case(instrument, evidence_text, events_text, market, fundamental, filing, news, cross)
        _emit_done(InvestigationStage.BUILDING_BEAR_CASE, bear.thesis[:80] + "…" if len(bear.thesis) > 80 else bear.thesis,
                   confidence=bear.confidence, downside_drivers=len(bear.downside_drivers))

        # ── Phase 5: Claims ──
        _emit_start(InvestigationStage.GENERATING_CLAIMS, "LLM generating structured claims")
        claims_raw = self._generate_claims(instrument, evidence_text, events_text, market, fundamental, filing, news, bull, bear, evidence_by_id.keys())
        _emit_done(InvestigationStage.GENERATING_CLAIMS, f"{len(claims_raw)} claims generated",
                   claims=len(claims_raw))

        # ── Phase 6: Citation Validation ──
        _emit_start(InvestigationStage.VALIDATING_CITATIONS, "Validating evidence citations")
        claims = self._validate_citations(claims_raw, all_evidence_ids)
        supported = sum(1 for c in claims if c.citation_status == "supported")
        unsupported = sum(1 for c in claims if c.citation_status == "unsupported")
        _emit_done(InvestigationStage.VALIDATING_CITATIONS,
                   f"{supported} supported, {unsupported} unsupported",
                   supported=supported, unsupported=unsupported, total=len(claims))

        # ── Phase 7: Critic ──
        _emit_start(InvestigationStage.RUNNING_CRITIC, "LLM critically reviewing all findings")
        critic = self._critique(instrument, evidence_text, bull, bear, claims, cross)
        _emit_done(InvestigationStage.RUNNING_CRITIC, f"{len(critic.findings)} critique findings",
                   findings=len(critic.findings))

        # ── Phase 8: Final Synthesis ──
        _emit_start(InvestigationStage.FINAL_SYNTHESIS, "LLM synthesizing final report")
        final = self._final_synthesis(instrument, evidence_text, events_text, plan, market, fundamental, filing, news, cross, bull, bear, claims, critic, context, warnings)
        _emit_done(InvestigationStage.FINAL_SYNTHESIS, "Final report generated")

        # ── Deterministic computations ──
        unknowns_list = list(context.unknowns_summary) + warnings
        claim_objs = tuple(
            Claim(
                claim_id=stable_id("llm_claim", instrument.symbol, c.claim_id or c.text[:40].replace(" ", "_")),
                text=c.text,
                claim_type=ClaimType(c.claim_type) if c.claim_type in {"observation", "inference", "hypothesis"} else ClaimType.OBSERVATION,
                evidence_ids=tuple(c.evidence_ids),
                inference_level=c.inference_level or "llm_interpretive",
                confidence=c.confidence,
            )
            for c in claims
        )
        citation_eval = evaluate_claim_citations(claim_objs, evidence)
        merged_quality = dict(quality_report or {})
        merged_quality["citation_support"] = citation_eval
        merged_quality["llm_context"] = {
            "context_id": context.context_id,
            "selected_count": len(context.selected_evidence),
            "excluded_count": context.excluded_count,
            "token_estimate": context.token_estimate,
        }
        # Structured case strengths for the trading fusion layer — these are
        # otherwise lost when the report is flattened to text lines.
        merged_quality["llm_cases"] = {
            "bull_confidence": bull.confidence,
            "bear_confidence": bear.confidence,
            "bull_thesis": bull.thesis,
            "bear_thesis": bear.thesis,
            "bull_catalysts": len(bull.catalysts),
            "bear_drivers": len(bear.downside_drivers),
            "bull_invalidation": list(bull.invalidation_conditions[:5]),
            "critic_severities": [f.severity for f in critic.findings],
        }

        confidence, reasons, breakdown = calculate_confidence_breakdown(
            evidence, unknowns_list, context.as_of,
            quality_report=merged_quality,
            citation_support_rate=float(citation_eval.get("citation_support_rate", 0.7)),
        )

        # ── Assemble report ──
        summary_lines = [final.get("executive_summary", ""), final.get("key_conclusion", "")]
        summary = "\n\n".join(line for line in summary_lines if line)

        bull_lines = bull.supporting_claims if bull.supporting_claims else [bull.thesis]
        bear_lines = bear.supporting_claims if bear.supporting_claims else [bear.thesis]

        risk_lines: list[str] = []
        for f in critic.findings:
            risk_lines.append(f"[{f.severity}] {f.description}")
        risk_lines.append(f"Invalidation (bull): {'; '.join(bull.invalidation_conditions[:3]) if bull.invalidation_conditions else 'none specified'}")
        risk_lines.append(f"Invalidation (bear): {'; '.join(bear.invalidation_conditions[:3]) if bear.invalidation_conditions else 'none specified'}")

        evidence_refs = []
        for e in detected_events[:5]:
            evidence_refs.extend(e.evidence_refs)
        if not evidence_refs:
            evidence_refs = [item.evidence_id for item in evidence[:8]]
        evidence_refs = list(dict.fromkeys(evidence_refs))[:8]

        discovered_unknowns: list[str] = []
        for u in final.get("discovered_unknowns", []):
            if isinstance(u, dict):
                discovered_unknowns.append(f"[{u.get('importance', '?')}] {u.get('description', str(u))}")
            else:
                discovered_unknowns.append(str(u))
        all_unknowns = unknowns_list + discovered_unknowns

        latency = (time.monotonic() - started) * 1000
        usage = self.provider.usage or LLMUsage()
        metadata = ResearchRunMetadata(
            research_mode="llm",
            llm_provider="openai_compatible",
            model=self.provider.config.model,
            prompt_versions={"version": "v1"},
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            latency_ms=latency,
            token_usage=usage,
            context_id=context.context_id,
            selected_evidence_count=len(context.selected_evidence),
            excluded_evidence_count=context.excluded_count,
        )

        report = ResearchReport(
            subject=instrument,
            generated_at=now_utc(),
            as_of=context.as_of,
            summary=summary or "LLM investigation completed.",
            bull_case=bull_lines,
            bear_case=bear_lines,
            risks=risk_lines,
            unknowns=all_unknowns,
            evidence_refs=tuple(evidence_refs),
            confidence=confidence,
            confidence_reasons=reasons,
            confidence_breakdown=breakdown,
            claims=claim_objs,
            quality_report=merged_quality,
            timeline=timeline,
            detected_events=detected_events,
        )

        self.audit.record(metadata)
        return report

    # ── Internal helpers ──

    def _call_structured(self, system: str, user: str, schema: dict, temperature: float = 0.2) -> dict:
        lang_instruction = self._lang_instruction()
        full_system = f"{system}\n\n{lang_instruction}"
        messages = [
            LLMMessage(role="system", content=full_system),
            LLMMessage(role="user", content=user),
        ]
        return self.provider.generate_structured(messages, schema, temperature=temperature)

    def _lang_instruction(self) -> str:
        if self._report_language == "zh-CN":
            return (
                "OUTPUT LANGUAGE: All user-facing analytical prose MUST be written in Simplified Chinese (简体中文). "
                "Preserve: ticker symbols (AAPL, NVDA), company legal names when necessary, "
                "evidence IDs (e.g., [E12]), numeric values, source URLs. "
                "Do NOT translate financial terminology key concepts."
            )
        return (
            "OUTPUT LANGUAGE: All user-facing analytical prose MUST be written in English. "
            "Preserve: ticker symbols, company legal names when necessary, "
            "evidence IDs (e.g., [E12]), numeric values, source URLs."
        )

    def _plan(self, symbol: str, company_name: str, evidence_text: str, events_text: str,
              unknowns: str, missing: str) -> ResearchPlan:
        system = (
            "You are a financial research planner. Formulate specific, evidence-grounded "
            "research questions about the company. Do NOT ask questions that cannot be "
            "answered with the evidence provided. If evidence is missing for a topic, "
            "note that explicitly."
        )
        user = (
            f"Company: {symbol} ({company_name})\n\n"
            f"## Evidence\n{evidence_text[:8000]}\n\n"
            f"## Events\n{events_text[:2000]}\n\n"
            f"## Unknowns: {unknowns}\n"
            f"## Missing modalities: {missing}\n\n"
            "Formulate 3-6 research questions. For each: id (Q1, Q2...), question text, "
            "priority (high/medium/low), required_evidence_types (list of market_bar/financial_fact/filing/news). "
            "Prioritize questions related to detected events. Mark questions as low priority "
            "if the required evidence modality is missing."
        )
        schema = {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "question": {"type": "string"},
                            "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                            "required_evidence_types": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["market_bar", "financial_fact", "filing", "news"]},
                            },
                        },
                        "required": ["id", "question", "priority", "required_evidence_types"],
                    },
                },
            },
            "required": ["questions"],
        }
        result = self._call_structured(system, user, schema)
        questions = [
            ResearchQuestion(
                id=q.get("id", f"Q{i}"),
                question=q.get("question", ""),
                priority=q.get("priority", "medium"),
                required_evidence_types=q.get("required_evidence_types", []),
            )
            for i, q in enumerate(result.get("questions", []))
        ]
        return ResearchPlan(questions=questions)

    def _analyze_market(self, instrument: Instrument, evidence_text: str,
                        events_text: str, plan: ResearchPlan) -> AnalysisResult:
        system = (
            "You analyze market data (price/volume) for a stock. "
            "Describe what the data shows about price movements and volume patterns. "
            "DO NOT calculate any numbers yourself — reference only what is in the evidence. "
            "Cite specific evidence IDs using [<id>] format. "
            "If market data is unavailable or limited, state that explicitly."
        )
        questions_str = "\n".join(
            f"- [{q.priority}] {q.question}" for q in plan.questions
            if any(t in {"market_bar"} for t in q.required_evidence_types)
        ) or "No specific market questions."
        user = (
            f"Symbol: {instrument.symbol}\n\n"
            f"## Market Evidence\n{evidence_text[:6000]}\n\n"
            f"## Events\n{events_text[:2000]}\n\n"
            f"## Relevant Questions\n{questions_str}\n\n"
            "Provide a structured market analysis."
        )
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "key_findings": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": ["summary", "key_findings", "evidence_refs", "confidence"],
        }
        result = self._call_structured(system, user, schema)
        return AnalysisResult(
            title="Market Analysis",
            summary=result.get("summary", ""),
            key_findings=result.get("key_findings", []),
            evidence_refs=result.get("evidence_refs", []),
            confidence=result.get("confidence", 0.0),
        )

    def _analyze_fundamentals(self, instrument: Instrument, evidence_text: str,
                              plan: ResearchPlan) -> AnalysisResult:
        system = (
            "You analyze fundamental financial data (revenue, profit, margins, cash flow, etc.) "
            "from SEC company facts. Reference ONLY the exact values shown in the evidence. "
            "DO NOT calculate growth rates or ratios yourself — if the evidence shows a number, "
            "you can reference it, but do not perform arithmetic. "
            "Cite specific evidence IDs using [<id>] format."
        )
        questions_str = "\n".join(
            f"- [{q.priority}] {q.question}" for q in plan.questions
            if any(t in {"financial_fact", "filing"} for t in q.required_evidence_types)
        ) or "Analyze available fundamental data."
        user = (
            f"Symbol: {instrument.symbol}\n\n"
            f"## Fundamental Evidence\n{evidence_text[:6000]}\n\n"
            f"## Relevant Questions\n{questions_str}\n\n"
            "Provide a structured fundamental analysis."
        )
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "key_findings": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": ["summary", "key_findings", "evidence_refs", "confidence"],
        }
        result = self._call_structured(system, user, schema)
        return AnalysisResult(
            title="Fundamental Analysis",
            summary=result.get("summary", ""),
            key_findings=result.get("key_findings", []),
            evidence_refs=result.get("evidence_refs", []),
            confidence=result.get("confidence", 0.0),
        )

    def _analyze_filings(self, instrument: Instrument, evidence_text: str,
                         plan: ResearchPlan) -> AnalysisResult:
        system = (
            "You analyze SEC filing data. Focus on: new filings, material changes, "
            "risk disclosures, and management information. Reference ONLY filing evidence "
            "in the provided context. Cite specific evidence IDs using [<id>] format."
        )
        user = (
            f"Symbol: {instrument.symbol}\n\n"
            f"## Filing Evidence\n{evidence_text[:5000]}\n\n"
            "Provide a structured filing analysis."
        )
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "key_findings": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": ["summary", "key_findings", "evidence_refs", "confidence"],
        }
        result = self._call_structured(system, user, schema)
        return AnalysisResult(
            title="Filing Analysis",
            summary=result.get("summary", ""),
            key_findings=result.get("key_findings", []),
            evidence_refs=result.get("evidence_refs", []),
            confidence=result.get("confidence", 0.0),
        )

    def _analyze_news(self, instrument: Instrument, evidence_text: str,
                      events_text: str, plan: ResearchPlan) -> AnalysisResult:
        system = (
            "You analyze financial news. Focus on: events, source agreement/disagreement, "
            "conflicting reports, first source vs syndication, and materiality. "
            "Cite specific evidence IDs using [<id>] format."
        )
        user = (
            f"Symbol: {instrument.symbol}\n\n"
            f"## News Evidence\n{evidence_text[:5000]}\n\n"
            f"## Events\n{events_text[:2000]}\n\n"
            "Provide a structured news analysis."
        )
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "key_findings": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": ["summary", "key_findings", "evidence_refs", "confidence"],
        }
        result = self._call_structured(system, user, schema)
        return AnalysisResult(
            title="News Analysis",
            summary=result.get("summary", ""),
            key_findings=result.get("key_findings", []),
            evidence_refs=result.get("evidence_refs", []),
            confidence=result.get("confidence", 0.0),
        )

    def _cross_source(self, instrument: Instrument, evidence_text: str,
                      events_text: str, market: AnalysisResult, fundamental: AnalysisResult,
                      filing: AnalysisResult, news: AnalysisResult) -> AnalysisResult:
        system = (
            "You synthesize insights across multiple evidence sources. Identify: "
            "1. Where do different sources agree? "
            "2. Where do they conflict? "
            "3. What patterns emerge across sources? "
            "4. What can be concluded that no single source shows alone? "
            "Cite evidence IDs using [<id>] format. DO NOT fabricate data."
        )
        user = (
            f"Symbol: {instrument.symbol}\n\n"
            f"## Market Analysis\n{market.summary}\n{chr(10).join(market.key_findings)}\n\n"
            f"## Fundamental Analysis\n{fundamental.summary}\n{chr(10).join(fundamental.key_findings)}\n\n"
            f"## Filing Analysis\n{filing.summary}\n{chr(10).join(filing.key_findings)}\n\n"
            f"## News Analysis\n{news.summary}\n{chr(10).join(news.key_findings)}\n\n"
            f"## Events\n{events_text[:2000]}\n\n"
            f"## Evidence References\n{evidence_text[:3000]}\n\n"
            "Provide cross-source synthesis."
        )
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "key_findings": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": ["summary", "key_findings", "evidence_refs", "confidence"],
        }
        result = self._call_structured(system, user, schema)
        return AnalysisResult(
            title="Cross-source Synthesis",
            summary=result.get("summary", ""),
            key_findings=result.get("key_findings", []),
            evidence_refs=result.get("evidence_refs", []),
            confidence=result.get("confidence", 0.0),
        )

    def _bull_case(self, instrument: Instrument, evidence_text: str, events_text: str,
                   market: AnalysisResult, fundamental: AnalysisResult, filing: AnalysisResult,
                   news: AnalysisResult, cross: AnalysisResult) -> BullCaseResult:
        system = (
            "You are a bullish analyst. Build a constructive investment thesis backed by "
            "specific evidence. Your output MUST include: "
            "1) thesis — a clear statement of the bull case "
            "2) supporting_claims — evidence-backed points supporting the thesis "
            "3) catalysts — what could drive the stock higher "
            "4) risks — what could go wrong with the bull case "
            "5) invalidation_conditions — what would prove the bull thesis WRONG "
            "Only use evidence in the provided context. Cite [<id>] references. "
            "If evidence is insufficient for a strong bull case, say so."
        )
        user = (
            f"Symbol: {instrument.symbol} ({instrument.company_name})\n\n"
            f"## Cross-source Analysis\n{cross.summary}\n\n"
            f"## Market\n{market.summary}\n\n"
            f"## Fundamental\n{fundamental.summary}\n\n"
            f"## Filing\n{filing.summary}\n\n"
            f"## News\n{news.summary}\n\n"
            f"## Evidence\n{evidence_text[:4000]}\n\n"
            "Construct the bull case."
        )
        schema = {
            "type": "object",
            "properties": {
                "thesis": {"type": "string"},
                "supporting_claims": {"type": "array", "items": {"type": "string"}},
                "catalysts": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "invalidation_conditions": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["thesis", "supporting_claims", "catalysts", "risks", "invalidation_conditions", "confidence", "evidence_refs"],
        }
        result = self._call_structured(system, user, schema)
        return BullCaseResult(
            thesis=result.get("thesis", ""),
            supporting_claims=result.get("supporting_claims", []),
            catalysts=result.get("catalysts", []),
            risks=result.get("risks", []),
            invalidation_conditions=result.get("invalidation_conditions", []),
            confidence=result.get("confidence", 0.0),
            evidence_refs=result.get("evidence_refs", []),
        )

    def _bear_case(self, instrument: Instrument, evidence_text: str, events_text: str,
                   market: AnalysisResult, fundamental: AnalysisResult, filing: AnalysisResult,
                   news: AnalysisResult, cross: AnalysisResult) -> BearCaseResult:
        system = (
            "You are a bearish analyst. Build a skeptical investment thesis backed by "
            "specific evidence. Include: thesis, supporting_claims, downside_drivers, "
            "contradictions (where bull case might be wrong), invalidation_conditions "
            "(what would prove the bear thesis WRONG). "
            "Only use evidence in the provided context. Cite [<id>] references."
        )
        user = (
            f"Symbol: {instrument.symbol} ({instrument.company_name})\n\n"
            f"## Cross-source Analysis\n{cross.summary}\n\n"
            f"## Market\n{market.summary}\n\n"
            f"## Fundamental\n{fundamental.summary}\n\n"
            f"## Filing\n{filing.summary}\n\n"
            f"## News\n{news.summary}\n\n"
            f"## Evidence\n{evidence_text[:4000]}\n\n"
            "Construct the bear case."
        )
        schema = {
            "type": "object",
            "properties": {
                "thesis": {"type": "string"},
                "supporting_claims": {"type": "array", "items": {"type": "string"}},
                "downside_drivers": {"type": "array", "items": {"type": "string"}},
                "contradictions": {"type": "array", "items": {"type": "string"}},
                "invalidation_conditions": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["thesis", "supporting_claims", "downside_drivers", "contradictions", "invalidation_conditions", "confidence", "evidence_refs"],
        }
        result = self._call_structured(system, user, schema)
        return BearCaseResult(
            thesis=result.get("thesis", ""),
            supporting_claims=result.get("supporting_claims", []),
            downside_drivers=result.get("downside_drivers", []),
            contradictions=result.get("contradictions", []),
            invalidation_conditions=result.get("invalidation_conditions", []),
            confidence=result.get("confidence", 0.0),
            evidence_refs=result.get("evidence_refs", []),
        )

    def _generate_claims(self, instrument: Instrument, evidence_text: str, events_text: str,
                         market: AnalysisResult, fundamental: AnalysisResult, filing: AnalysisResult,
                         news: AnalysisResult, bull: BullCaseResult, bear: BearCaseResult,
                         evidence_ids: Iterable[str] = ()) -> list[ValidatedClaim]:
        system = (
            "Generate structured claims about the company based on provided evidence. "
            "Each claim must include: claim_id, text, claim_type (observation/inference/hypothesis), "
            "evidence_ids (copy ids VERBATIM from the citable id list; do not reformat them), "
            "inference_level (direct/interpretive/hypothesis), confidence (0-1), caveats. "
            "OBSERVATIONS must have direct evidence backing. INFERENCES are reasoned from evidence. "
            "HYPOTHESES are speculative. DO NOT fabricate evidence IDs."
        )
        user = (
            f"Symbol: {instrument.symbol}\n"
            f"Market: {market.summary}\n"
            f"Fundamental: {fundamental.summary}\n"
            f"Filing: {filing.summary}\n"
            f"News: {news.summary}\n"
            f"Bull: {bull.thesis}\n"
            f"Bear: {bear.thesis}\n"
            f"Citable evidence ids (use these exact strings): "
            f"{self._evidence_id_roster(evidence_ids)}\n\n"
            f"Evidence detail: {evidence_text[:2000]}\n\n"
            "Generate 8-15 claims."
        )
        schema = {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim_id": {"type": "string"},
                            "text": {"type": "string"},
                            "claim_type": {"type": "string", "enum": ["observation", "inference", "hypothesis"]},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "inference_level": {"type": "string"},
                            "confidence": {"type": "number"},
                            "caveats": {"type": "string"},
                        },
                        "required": ["claim_id", "text", "claim_type", "evidence_ids", "inference_level", "confidence", "caveats"],
                    },
                },
            },
            "required": ["claims"],
        }
        result = self._call_structured(system, user, schema)
        claims = []
        for c in result.get("claims", []):
            claims.append(ValidatedClaim(
                claim_id=c.get("claim_id", ""),
                text=c.get("text", ""),
                claim_type=c.get("claim_type", "observation"),
                evidence_ids=c.get("evidence_ids", []),
                inference_level=c.get("inference_level", "interpretive"),
                confidence=c.get("confidence", 0.5),
                caveats=c.get("caveats", ""),
                citation_status="pending",
            ))
        return claims

    @staticmethod
    def _evidence_id_roster(evidence_ids: Iterable[str]) -> str:
        """Complete, compact list of citable ids.

        The claims prompt used to pass truncated evidence prose, so the model was
        asked for 8-15 claims while seeing only the first few ids and had to
        invent the rest. The roster is never truncated.
        """
        return ", ".join(f"[{eid}]" for eid in evidence_ids)

    @staticmethod
    def _resolve_ref(ref: str, lookup: dict[str, str]) -> str | None:
        """Map a citation as the LLM wrote it back to a canonical evidence_id.

        Tolerates the cosmetic variance models introduce when copying a token out
        of a prompt (brackets, whitespace, case, a stray `E` prefix left over from
        the old `[<id>]` rendering). Anything that does not resolve to a real id
        is a fabrication and must stay rejected.
        """
        token = "".join(ch for ch in ref if ch not in "[]() \t").lower()
        if token in lookup:
            return lookup[token]
        # try this only after the raw form: evidence_id is sha256 hex and may
        # legitimately begin with 'e', which must not be stripped away.
        if token.startswith("e") and token[1:] in lookup:
            return lookup[token[1:]]
        return None

    def _validate_citations(self, claims: list[ValidatedClaim],
                            valid_ids: set[str]) -> list[ValidatedClaim]:
        lookup = {vid.lower(): vid for vid in valid_ids}
        validated = []
        for c in claims:
            valid_refs: list[str] = []
            invalid_refs: list[str] = []
            for ref in c.evidence_ids:
                resolved = self._resolve_ref(ref, lookup)
                if resolved is None:
                    invalid_refs.append(ref)
                elif resolved not in valid_refs:
                    valid_refs.append(resolved)

            if not valid_refs:
                # no references at all, or every reference was fabricated
                status = "unsupported"
            elif invalid_refs:
                status = "partially_supported"
            else:
                status = "supported"

            validated.append(ValidatedClaim(
                claim_id=c.claim_id,
                text=c.text,
                claim_type=c.claim_type,
                evidence_ids=valid_refs,
                inference_level=c.inference_level,
                confidence=c.confidence * 0.5 if status == "unsupported" else c.confidence,
                caveats=c.caveats + (f" [INVALID_REFS: {','.join(invalid_refs)}]" if invalid_refs else ""),
                citation_status=status,
            ))
        return validated

    def _critique(self, instrument: Instrument, evidence_text: str,
                  bull: BullCaseResult, bear: BearCaseResult,
                  claims: list[ValidatedClaim], cross: AnalysisResult) -> CritiqueResult:
        system = (
            "You are a research critic. Your job is to find weaknesses. Check for: "
            "unsupported claims, weak evidence, stale evidence, evidence concentration "
            "(too many claims from one source), source dependence, causal overreach "
            "(correlation mistaken for causation), missing counter evidence, "
            "conflicting evidence, price-in possibility (is this already priced in?), "
            "narrative bias. Challenge both bull and bear theses. "
            "If a claim has no evidence, flag it. If evidence is stale, flag it."
        )
        unsupported = [c for c in claims if c.citation_status == "unsupported"]
        user = (
            f"Symbol: {instrument.symbol}\n\n"
            f"## Bull Thesis\n{bull.thesis}\n{bull.supporting_claims}\n\n"
            f"## Bear Thesis\n{bear.thesis}\n{bear.supporting_claims}\n\n"
            f"## Cross-source\n{cross.summary}\n\n"
            f"## Claims ({len(claims)} total, {len(unsupported)} unsupported)\n"
            + "\n".join(f"- [{c.citation_status}] {c.text[:120]}" for c in claims[:15])
            + "\n\nProvide a structured critique."
        )
        schema = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "description": {"type": "string"},
                            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        },
                        "required": ["type", "description", "severity"],
                    },
                },
                "overall_assessment": {"type": "string"},
            },
            "required": ["findings", "overall_assessment"],
        }
        result = self._call_structured(system, user, schema)
        findings = [
            CritiqueFinding(
                type=f.get("type", "unknown"),
                description=f.get("description", ""),
                severity=f.get("severity", "medium"),
            )
            for f in result.get("findings", [])
        ]
        return CritiqueResult(
            findings=findings,
            overall_assessment=result.get("overall_assessment", ""),
        )

    def _final_synthesis(self, instrument: Instrument, evidence_text: str, events_text: str,
                         plan: ResearchPlan, market: AnalysisResult, fundamental: AnalysisResult,
                         filing: AnalysisResult, news: AnalysisResult, cross: AnalysisResult,
                         bull: BullCaseResult, bear: BearCaseResult,
                         claims: list[ValidatedClaim], critic: CritiqueResult,
                         context: ResearchContext, warnings: list[str]) -> dict:
        system = (
            "You synthesize a complete research report executive summary. "
            "Answer: what happened? What is the most likely explanation? "
            "What evidence is most critical? What remains uncertain? "
            "DO NOT fabricate data. Reference evidence IDs. "
            "Also list discovered unknowns: what do we still not know?"
        )
        user = (
            f"Symbol: {instrument.symbol} ({instrument.company_name})\n\n"
            f"## Market\n{market.summary}\n\n"
            f"## Fundamental\n{fundamental.summary}\n\n"
            f"## Filing\n{filing.summary}\n\n"
            f"## News\n{news.summary}\n\n"
            f"## Cross-source\n{cross.summary}\n\n"
            f"## Bull Case\n{bull.thesis}\n\n"
            f"## Bear Case\n{bear.thesis}\n\n"
            f"## Critic\n{critic.overall_assessment}\n\n"
            f"## Key Claims\n" + "\n".join(f"- {c.text[:150]}" for c in claims[:8]) + "\n\n"
            f"## Warnings: {'; '.join(warnings) if warnings else 'none'}\n"
            f"## Missing modalities: {', '.join(context.missing_modalities) or 'none'}\n\n"
            "Provide the final executive summary and discovered unknowns."
        )
        schema = {
            "type": "object",
            "properties": {
                "executive_summary": {"type": "string"},
                "key_conclusion": {"type": "string"},
                "discovered_unknowns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "category": {"type": "string"},
                            "importance": {"type": "string"},
                        },
                        "required": ["description", "category", "importance"],
                    },
                },
            },
            "required": ["executive_summary", "key_conclusion", "discovered_unknowns"],
        }
        return self._call_structured(system, user, schema)
