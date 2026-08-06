# ADR-011 Observability

## Context

Need audit, replay, debugging, source provenance, model/prompt versioning.

## Options

- Logs only.
- Structured run/event/audit tables.
- Full OpenTelemetry stack.

## Evidence

Lean/Freqtrade persist order/trade states; Qlib uses recorder; LLM research needs model/prompt/source snapshots.

## Trade-offs

Full observability stack is premature; logs only are insufficient.

## Decision

Use structured audit tables from Phase 1; add OpenTelemetry later.

## Rejected Alternatives

Plain text logs as primary audit.

## Confidence

High.

## Revisit Condition

When multiple services/workers appear.

