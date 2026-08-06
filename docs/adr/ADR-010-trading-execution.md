# ADR-010 Trading Execution

## Context

Future paper/live trading requires deterministic OMS and broker adapters.

## Options

- LLM direct broker calls.
- Custom minimal OMS + broker adapter.
- Lean/Nautilus execution engine.

## Evidence

Lean `BrokerageTransactionHandler.cs`, `Order.cs`, `OrderEvent.cs`; Nautilus `SubmitOrder`, `OrderManager`; Freqtrade exchange layer all separate order lifecycle from strategy.

## Trade-offs

Custom minimal OMS is feasible for paper; heavy engines safer later.

## Decision

Never allow LLM direct broker calls. Start custom minimal OMS for paper spike; revisit Lean/Nautilus for live.

## Rejected Alternatives

Natural-language order execution.

## Confidence

High.

## Revisit Condition

Before Phase 4 paper trading.

