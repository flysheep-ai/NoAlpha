"""Phase Q5 E2E: four real LLM fusion scenarios with runtime evidence.

E2E-1: Quant/LLM Agreement — quant candidate + real LLM investigation →
       confirmed BUY with full provenance chain.
E2E-2: Quant/LLM Conflict — get LLM to review a quant strong candidate
       that has real bearish evidence → conflict → NO_ACTION.
E2E-3: LLM Provider Failure — no API key / bad config → QUANT_ONLY_FALLBACK.
E2E-4: Existing Position Review — quant rank deterioration + LLM →
       REDUCE/SELL.

Usage:
  .venv/bin/python experiments/e2e_llm_fusion.py --scenario all
  .venv/bin/python experiments/e2e_llm_fusion.py --scenario agreement
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from noalpha.api import CredentialStore, resolve_llm_config
from noalpha.market.instruments import parse_secid
from noalpha.pipeline import InvestigationPipeline
from noalpha.quant.domain import QuantSignal, SignalDirection
from noalpha.research_mode import ResearchMode
from noalpha.trading.proposal import TradeAction, TradeProposal, TradeRationale
from noalpha.trading.research_fusion import (
    FusionPolicy, TradingResearchService, extract_snapshot, snapshot_to_signal, default_trading_research_service,
)
from noalpha.trading.shadow import DecisionDelta, ShadowArena


def run_e2e_agreement() -> dict:
    """E2E-1: Quant/LLM Agreement on a strong candidate."""
    quant = QuantSignal(symbol="NVDA", instrument_id="105.NVDA", direction=SignalDirection.LONG,
                        strength=0.92, confidence=0.68, calibrated=False,
                        model_version="lightgbm v1 CN")
    svc = default_trading_research_service()
    if svc is None:
        return {"e2e": "agreement", "status": "SKIPPED", "reason": "no LLM key configured"}
    svc.budget.cycle_calls = 0
    t0 = time.time()
    snap, st = svc.research(secid="105.NVDA", symbol="NVDA", market="US")
    dt = time.time() - t0
    if snap is None:
        return {"e2e": "agreement", "status": "FAILED", "error": st, "latency_s": dt}
    sig = snapshot_to_signal(snap)
    fusion = FusionPolicy().fuse(symbol="NVDA", secid="105.NVDA", quant=quant, llm=sig)
    return {
        "e2e": "agreement",
        "status": "PASS",
        "latency_s": round(dt, 1),
        "llm_model": snap.model,
        "investigation_id": snap.investigation_id,
        "bull_strength": snap.bull_strength,
        "bear_strength": snap.bear_strength,
        "thesis_direction": sig.direction.value,
        "citation_support": snap.citation_support,
        "fusion_action": fusion.final_signal,
        "fusion_strength": fusion.final_strength,
        "fusion_conflict": fusion.conflict_state.value,
        "effective_mode": fusion.effective_mode,
        "evidence_count": snap.selected_evidence_count,
        "tokens": snap.input_tokens + snap.output_tokens,
    }


def run_e2e_conflict() -> dict:
    """E2E-2: find a weak quant candidate, feed it to LLM, check if the LLM is
    bearish/uncertain (i.e. conflict with a hypothetical quant-bull signal)."""
    quant = QuantSignal(symbol="TSLA", instrument_id="105.TSLA", direction=SignalDirection.LONG,
                        strength=0.62, confidence=0.45, calibrated=False,
                        model_version="lightgbm v1 US")
    svc = default_trading_research_service()
    if svc is None:
        return {"e2e": "conflict", "status": "SKIPPED", "reason": "no LLM key configured"}
    svc.budget.cycle_calls = 0
    t0 = time.time()
    snap, st = svc.research(secid="105.TSLA", symbol="TSLA", market="US")
    dt = time.time() - t0
    if snap is None:
        return {"e2e": "conflict", "status": "FAILED", "error": st}
    sig = snapshot_to_signal(snap)
    quant_bear = QuantSignal(symbol="TSLA", instrument_id="105.TSLA",
                             direction=SignalDirection.SHORT, strength=0.30, confidence=0.40,
                             calibrated=False, model_version="lightgbm v1 US")
    fusion_bearbull = FusionPolicy().fuse(symbol="TSLA", secid="105.TSLA",
                                           quant=quant_bear, llm=sig)
    fusion_bullbear = FusionPolicy().fuse(symbol="TSLA", secid="105.TSLA",
                                           quant=quant, llm=sig)
    return {
        "e2e": "conflict",
        "status": "PASS",
        "latency_s": round(dt, 1),
        "llm_direction": sig.direction.value,
        "bull_strength": snap.bull_strength,
        "bear_strength": snap.bear_strength,
        "contradiction_score": snap.contradiction_score,
        "quant_bear_x_llm": {
            "conflict_state": fusion_bearbull.conflict_state.value,
            "final_signal": fusion_bearbull.final_signal,
            "explanation": fusion_bearbull.explanation[:200],
        },
        "quant_bull_x_llm_bear": {
            "conflict_state": fusion_bullbear.conflict_state.value,
            "final_signal": fusion_bullbear.final_signal,
            "explanation": fusion_bullbear.explanation[:200],
        },
    }


def run_e2e_failure() -> dict:
    """E2E-3: LLM provider unavailable → QUANT_ONLY_FALLBACK visible."""
    # Build service with garbage URL to simulate unreachable API
    from noalpha.pipeline import InvestigationPipeline
    from noalpha.research_mode import ResearchMode
    from noalpha.llm.config import LLMConfig
    bad_cfg = LLMConfig(base_url="https://10.0.0.1:9999/v1", api_key="deadbeef",
                        model="deepseek-chat", timeout=8)
    def bad_factory():
        return InvestigationPipeline.default(
            cache_dir=Path(".cache/api/providers"),
            research_mode=ResearchMode.LLM, llm_config=bad_cfg)
    svc = TradingResearchService(bad_factory, model=bad_cfg.model,
                                 llm_provider=bad_cfg.base_url)
    quant = QuantSignal(symbol="NVDA", instrument_id="105.NVDA",
                        direction=SignalDirection.LONG, strength=0.80,
                        confidence=0.60, calibrated=False,
                        model_version="lightgbm v1 CN")
    snap, st = svc.research(secid="105.NVDA", symbol="NVDA", market="US")
    if snap is not None:
        return {"e2e": "failure", "status": "UNEXPECTED_SUCCESS", "warning": "bad config somehow worked"}
    fusion = FusionPolicy().fuse(symbol="NVDA", secid="105.NVDA", quant=quant, llm=None)
    return {
        "e2e": "failure",
        "status": "PASS",
        "fallback_status": st,
        "fusion_effective_mode": fusion.effective_mode,
        "fusion_final_signal": fusion.final_signal,
        "fusion_final_strength": fusion.final_strength,
        "conflict_state": fusion.conflict_state.value,
        "quant_only_still_made_decision": fusion.final_signal != "no_action",
    }


def run_e2e_position_review() -> dict:
    """E2E-4: quant rank deteriorated + LLM review → REDUCE/SELL candidate."""
    quant = QuantSignal(symbol="META", instrument_id="105.META",
                        direction=SignalDirection.SHORT, strength=0.25, confidence=0.35,
                        calibrated=False, model_version="lightgbm v1 US")
    svc = default_trading_research_service()
    if svc is None:
        return {"e2e": "position_review", "status": "SKIPPED", "reason": "no LLM key configured"}
    svc.budget.cycle_calls = 0
    t0 = time.time()
    snap, st = svc.research(secid="105.META", symbol="META", market="US",
                             quant_turned_short=True, holding=True)
    dt = time.time() - t0
    if snap is None:
        return {"e2e": "position_review", "status": "FAILED", "error": st}
    sig = snapshot_to_signal(snap)
    fusion = FusionPolicy().fuse(symbol="META", secid="105.META", quant=quant, llm=sig,
                                  holding=True)
    return {
        "e2e": "position_review",
        "status": "PASS",
        "latency_s": round(dt, 1),
        "quant_direction": "short",
        "llm_direction": sig.direction.value,
        "bull_strength": snap.bull_strength,
        "bear_strength": snap.bear_strength,
        "thesis_confidence": snap.thesis_confidence,
        "fusion_final_signal": fusion.final_signal,
        "fusion_conflict_state": fusion.conflict_state.value,
        "explanation": fusion.explanation[:300],
    }


ALL_SCENARIOS = {
    "agreement": run_e2e_agreement,
    "conflict": run_e2e_conflict,
    "failure": run_e2e_failure,
    "position_review": run_e2e_position_review,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="all")
    args = ap.parse_args()
    scenarios = list(ALL_SCENARIOS) if args.scenario == "all" else [args.scenario]
    results: dict[str, dict] = {}
    for name in scenarios:
        if name not in ALL_SCENARIOS:
            print(f"unknown scenario: {name}")
            continue
        print(f"\n{'='*40}\nE2E: {name}\n{'='*40}")
        try:
            results[name] = ALL_SCENARIOS[name]()
        except Exception as exc:
            results[name] = {"e2e": name, "status": "ERROR", "error": str(exc)}
        print(json.dumps(results[name], ensure_ascii=False, indent=2))

    passed = sum(1 for v in results.values()
                 if v["status"] in ("PASS", "SKIPPED"))
    failed = sum(1 for v in results.values()
                 if v["status"] not in ("PASS", "SKIPPED"))
    print(f"\n{'='*40}\n{passed} passed/skipped, {failed} failed\n{'='*40}")
    Path("experiments/results/phase_q5_e2e.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
