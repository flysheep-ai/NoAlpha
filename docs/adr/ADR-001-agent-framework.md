# ADR-001 Agent Framework

## Context

Need multi-step research workflows with evidence, critique, and optional human review.

## Options

- Plain service/workflow code.
- LangGraph.
- TradingAgents fork.

## Evidence

TradingAgents uses LangGraph in `tradingagents/graph/setup.py` and `trading_graph.py`; tests include checkpoint/resume. But project-specific state/memory still needs full read.

## Trade-offs

LangGraph gives state/checkpoint patterns but adds dependency and complexity.

## Decision

NEEDS_MORE_RESEARCH. Phase 1 should start with plain deterministic workflow; LangGraph spike required before adoption.

## Rejected Alternatives

Forking TradingAgents directly.

## Confidence

Medium.

## Revisit Condition

After `experiments/agent_research` validates checkpoint/replay and human review.

