# ADR-006 Financial Documents

## Context

Need SEC filings and later A/HK announcements.

## Options

- Raw SEC API client.
- EdgarTools.
- sec-parser for text sections.

## Evidence

SEC companyfacts spike succeeded. EdgarTools has HTTP/rate-limit/XBRL modules and active commits.

## Trade-offs

EdgarTools saves substantial parsing work but does not replace PIT warehouse.

## Decision

Adopt EdgarTools for SEC spike; wrap it behind internal interfaces.

## Rejected Alternatives

Only raw requests.

## Confidence

High for SEC; low for A/HK announcements.

## Revisit Condition

If EdgarTools cannot preserve needed metadata or has performance issues.

