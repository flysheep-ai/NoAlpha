# ADR-005 News Pipeline

## Context

Need legal, timestamped news for event clustering.

## Options

- RSS/free web scraping.
- Finnhub/NewsAPI/Benzinga.
- LSEG/Bloomberg/AlphaSense.

## Evidence

No provider has been tested. Copyright and redistribution constraints are high.

## Trade-offs

Commercial sources solve coverage but cost; scraping is risky.

## Decision

NEEDS_MORE_RESEARCH. Prototype clustering with synthetic/public sample data only.

## Rejected Alternatives

Unlicensed scraping of paid news/research.

## Confidence

Low.

## Revisit Condition

After selecting a legal news API and testing timestamps.

