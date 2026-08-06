# ADR-009 Backtesting Engine

## Context

Need PIT backtesting for signals and later paper/live parity.

## Options

- Simple custom backtester.
- Qlib.
- Lean.
- NautilusTrader.

## Evidence

Lean has full transaction/order/risk architecture. Qlib has ML research backtest loop. Nautilus has high-performance matching/order manager.

## Trade-offs

Lean/Nautilus are powerful but heavy. Custom is fast but risks subtle bias.

## Decision

NEEDS_MORE_RESEARCH. Build minimal spike first; evaluate Lean local backtest second.

## Rejected Alternatives

Choosing a heavy engine before PIT data model is defined.

## Confidence

Low/Medium.

## Revisit Condition

After `experiments/backtesting`.

