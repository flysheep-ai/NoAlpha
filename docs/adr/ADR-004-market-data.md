# ADR-004 Market Data

## Context

Need A/HK/US market data with legal use and predictable latency.

## Options

- Free web endpoints.
- OpenBB/AKShare wrappers.
- Paid vendors/Broker data.

## Evidence

Yahoo chart spike timed out; AKShare/OpenBB not yet live-tested. Market data authorization remains unresolved.

## Trade-offs

Free endpoints are fast to prototype but unreliable and legally uncertain.

## Decision

NEEDS_MORE_RESEARCH. Phase 1 may use delayed/free data for research only, clearly labeled non-production.

## Rejected Alternatives

Using Yahoo/yfinance as production realtime.

## Confidence

Low.

## Revisit Condition

After market_data spike tests AKShare/Tushare/Finnhub/Polygon/Broker options.

