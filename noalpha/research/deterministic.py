from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from noalpha.confidence import calculate_confidence_breakdown
from noalpha.domain import (
    AsOfContext,
    Claim,
    ClaimType,
    DetectedEvent,
    Evidence,
    EvidenceType,
    Instrument,
    ResearchReport,
    TimelineEvent,
    now_utc,
    stable_id,
)
from noalpha.quality import evaluate_claim_citations


class ResearchOrchestrator:
    def build_report(
        self,
        instrument: Instrument,
        context: AsOfContext,
        evidence: list[Evidence],
        timeline: tuple[TimelineEvent, ...],
        detected_events: tuple[DetectedEvent, ...],
        provider_warnings: list[str] | None = None,
        quality_report: dict | None = None,
        report_language: str = "en-US",
    ) -> ResearchReport:
        zh = report_language == "zh-CN"
        warnings = provider_warnings or []
        unknowns = self._unknowns(evidence, warnings, zh)
        bull = self._bull_case(instrument, evidence, detected_events, zh)
        bear = self._bear_case(instrument, evidence, detected_events, zh)
        risks = self._risk_critique(evidence, detected_events, unknowns, zh)
        summary_refs = self._top_refs(evidence, detected_events)
        summary = self._summary(instrument, evidence, detected_events, summary_refs, zh)
        claims = self._build_claims(instrument, evidence, detected_events, bull, bear, risks)
        citation_eval = evaluate_claim_citations(claims, evidence)
        merged_quality = dict(quality_report or {})
        merged_quality["citation_support"] = citation_eval
        confidence, reasons, breakdown = calculate_confidence_breakdown(
            evidence,
            unknowns,
            context.as_of,
            quality_report=merged_quality,
            citation_support_rate=float(citation_eval["citation_support_rate"]),
        )
        return ResearchReport(
            subject=instrument,
            generated_at=now_utc(),
            as_of=context.as_of,
            summary=summary,
            bull_case=bull,
            bear_case=bear,
            risks=risks,
            unknowns=unknowns,
            evidence_refs=tuple(summary_refs),
            confidence=confidence,
            confidence_reasons=reasons,
            confidence_breakdown=breakdown,
            claims=claims,
            quality_report=merged_quality,
            timeline=timeline,
            detected_events=detected_events,
        )

    def _summary(self, instrument: Instrument, evidence: list[Evidence], events: tuple[DetectedEvent, ...], refs: list[str], zh: bool = False) -> str:
        filing_count = sum(1 for item in evidence if item.evidence_type == EvidenceType.FILING)
        news_count = sum(1 for item in evidence if item.evidence_type == EvidenceType.NEWS)
        fact_count = sum(1 for item in evidence if item.evidence_type == EvidenceType.FINANCIAL_FACT)
        event_count = len(events)
        citation = " ".join(f"[{ref}]" for ref in refs[:4])
        if zh:
            return (
                f"{instrument.symbol} 本次调查使用了 {fact_count} 条财务事实、{filing_count} 份披露文件、"
                f"{news_count} 条新闻，检测到 {event_count} 个事件。{citation}".strip()
            )
        return (
            f"{instrument.symbol} investigation used {fact_count} financial facts, "
            f"{filing_count} filings, {news_count} news items, and detected {event_count} events. {citation}".strip()
        )

    def _bull_case(self, instrument: Instrument, evidence: list[Evidence], events: tuple[DetectedEvent, ...], zh: bool = False) -> list[str]:
        lines: list[str] = []
        latest_revenue = self._latest_fact(evidence, {"Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"})
        latest_income = self._latest_fact(evidence, {"NetIncomeLoss", "OperatingIncomeLoss"})
        if latest_revenue:
            lines.append(
                f"最新报告期存在营收证据 [{latest_revenue.evidence_id}]。" if zh
                else f"Reported revenue evidence is available for the latest period [{latest_revenue.evidence_id}]."
            )
        if latest_income:
            value = Decimal(str(latest_income.metadata["value"]))
            if value > 0:
                lines.append(
                    f"最新盈利事实为正值 [{latest_income.evidence_id}]。" if zh
                    else f"Profitability evidence is positive in latest reported fact [{latest_income.evidence_id}]."
                )
        positive_events = [event for event in events if event.event_type.value in {"abnormal_volume", "new_filing"}]
        if positive_events:
            refs = ", ".join(f"[{ref}]" for ref in positive_events[0].evidence_refs[:2])
            lines.append(
                f"近期活动提供了值得调查的催化剂线索 {refs}。" if zh
                else f"Recent activity provides fresh evidence to investigate catalysts {refs}."
            )
        if lines:
            return lines
        return ["现有证据不足以支撑看涨结论；无引用时不应推断利好。"] if zh else \
            ["No strong bull case from available evidence; do not infer positives without citations."]

    def _bear_case(self, instrument: Instrument, evidence: list[Evidence], events: tuple[DetectedEvent, ...], zh: bool = False) -> list[str]:
        lines: list[str] = []
        latest_income = self._latest_fact(evidence, {"NetIncomeLoss", "OperatingIncomeLoss"})
        if latest_income:
            value = Decimal(str(latest_income.metadata["value"]))
            if value < 0:
                lines.append(
                    f"最新盈利事实为负值 [{latest_income.evidence_id}]。" if zh
                    else f"Latest profitability evidence is negative [{latest_income.evidence_id}]."
                )
        return_events = [event for event in events if event.event_type.value == "abnormal_return"]
        for event in return_events[:2]:
            refs = ", ".join(f"[{ref}]" for ref in event.evidence_refs[:2])
            lines.append(
                f"价格异动需要解释：{event.description} {refs}。" if zh
                else f"Price movement needs explanation: {event.description} {refs}."
            )
        if lines:
            return lines
        return ["现有证据不足以支撑看跌结论；关键下行风险可能缺失。"] if zh else \
            ["No strong bear case from available evidence; key downside risks may be missing."]

    def _risk_critique(self, evidence: list[Evidence], events: tuple[DetectedEvent, ...], unknowns: list[str], zh: bool = False) -> list[str]:
        lines = []
        if unknowns:
            prefix = "重要数据缺失限制了结论质量：" if zh else "Important missing data limits conclusion quality: "
            lines.append(prefix + "; ".join(unknowns[:3]))
        if any(item.source == "Yahoo Finance prototype chart" for item in evidence):
            refs = [item.evidence_id for item in evidence if item.source == "Yahoo Finance prototype chart"][:2]
            refs_str = " ".join(f"[{ref}]" for ref in refs)
            lines.append(
                f"行情数据来自原型数据源，不得当作生产级实时数据 {refs_str}。" if zh
                else f"Market data uses a prototype provider and must not be treated as production realtime {refs_str}."
            )
        if any(item.source == "MarketData.app prototype candles" for item in evidence):
            refs = [item.evidence_id for item in evidence if item.source == "MarketData.app prototype candles"][:2]
            refs_str = " ".join(f"[{ref}]" for ref in refs)
            lines.append(
                f"行情数据来自无 token 覆盖有限的原型日K数据源 {refs_str}。" if zh
                else f"Market data uses a prototype daily-bar provider with limited no-token coverage {refs_str}."
            )
        if not events:
            lines.append(
                "未触发任何确定性事件检测；叙事性结论应保持保守。" if zh
                else "No deterministic event trigger fired; narrative claims should stay conservative."
            )
        lines.append(
            "本阶段不授权 LLM 直接交易或设定硬性风控参数。" if zh
            else "LLM is not authorized to trade or set hard risk limits in this sprint."
        )
        return lines

    def _unknowns(self, evidence: list[Evidence], warnings: list[str], zh: bool = False) -> list[str]:
        types = {item.evidence_type for item in evidence}
        unknowns: list[str] = []
        if EvidenceType.MARKET_BAR not in types:
            unknowns.append("行情数据不可用或被 as-of 时点防线过滤。" if zh else "Market data unavailable or filtered by as-of guard.")
        if EvidenceType.FINANCIAL_FACT not in types:
            unknowns.append("SEC 财务事实不可用或被 as-of 时点防线过滤。" if zh else "SEC company facts unavailable or filtered by as-of guard.")
        if EvidenceType.FILING not in types:
            unknowns.append("最新披露文件不可用或被 as-of 时点防线过滤。" if zh else "Latest filings unavailable or filtered by as-of guard.")
        if EvidenceType.NEWS not in types:
            unknowns.append("新闻源不可用或窗口内没有带时间戳的条目。" if zh else "News source unavailable or no timestamped items in window.")
        unknowns.extend(warnings)
        return unknowns

    def _latest_fact(self, evidence: list[Evidence], concepts: set[str]) -> Evidence | None:
        facts = [
            item
            for item in evidence
            if item.evidence_type == EvidenceType.FINANCIAL_FACT and item.metadata.get("concept") in concepts
        ]
        if not facts:
            return None
        return max(facts, key=lambda item: (item.event_time, item.available_at))

    def _top_refs(self, evidence: list[Evidence], events: tuple[DetectedEvent, ...]) -> list[str]:
        refs: list[str] = []
        for event in events[:5]:
            refs.extend(event.evidence_refs)
        if not refs:
            refs.extend(item.evidence_id for item in evidence[:5])
        deduped = []
        for ref in refs:
            if ref not in deduped:
                deduped.append(ref)
        return deduped[:8]

    def _build_claims(
        self,
        instrument: Instrument,
        evidence: list[Evidence],
        events: tuple[DetectedEvent, ...],
        bull: list[str],
        bear: list[str],
        risks: list[str],
    ) -> tuple[Claim, ...]:
        claims: list[Claim] = []
        counts = defaultdict(int)
        for item in evidence:
            counts[item.evidence_type.value] += 1
        refs_by_type: dict[EvidenceType, list[str]] = defaultdict(list)
        for item in evidence:
            refs_by_type[item.evidence_type].append(item.evidence_id)
        for kind, count in sorted(counts.items()):
            ref_type = EvidenceType(kind)
            refs = tuple(refs_by_type[ref_type][:3])
            claims.append(
                Claim(
                    claim_id=stable_id("claim", instrument.symbol, "count", kind),
                    text=f"{instrument.symbol} investigation contains {count} canonical {kind} evidence items.",
                    claim_type=ClaimType.OBSERVATION,
                    evidence_ids=refs,
                    inference_level="direct",
                    confidence=0.9 if refs else 0.1,
                )
            )
        for event in events[:8]:
            claims.append(
                Claim(
                    claim_id=stable_id("claim", instrument.symbol, "event", event.event_id),
                    text=event.description,
                    claim_type=ClaimType.OBSERVATION,
                    evidence_ids=event.evidence_refs,
                    inference_level="deterministic_detection",
                    confidence=round(max(0.1, min(0.95, event.severity)), 2),
                )
            )
        for idx, line in enumerate(bull[:4]):
            claims.append(self._role_claim(instrument, "bull", idx, line, ClaimType.INFERENCE))
        for idx, line in enumerate(bear[:4]):
            claims.append(self._role_claim(instrument, "bear", idx, line, ClaimType.INFERENCE))
        for idx, line in enumerate(risks[:4]):
            claims.append(self._role_claim(instrument, "risk", idx, line, ClaimType.HYPOTHESIS))
        return tuple(claims)

    def _role_claim(self, instrument: Instrument, role: str, idx: int, text: str, claim_type: ClaimType) -> Claim:
        refs = tuple(part.strip("[].,") for part in text.split() if part.startswith("[") and part.rstrip(".,").endswith("]"))
        return Claim(
            claim_id=stable_id("claim", instrument.symbol, role, idx, text),
            text=text,
            claim_type=claim_type,
            evidence_ids=refs,
            inference_level="interpretive" if claim_type == ClaimType.INFERENCE else "hypothesis",
            confidence=0.65 if refs else 0.35,
        )
